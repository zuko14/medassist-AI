# SESSION 44 — G2 In-House Lab Information System (LIS)

**Date**: 2026-10-28
**Branch**: `hardening/production-readiness-audit` (uncommitted; not deployed)
**Migration**: `118_lab_information_system.sql` (+ `rollback/118_down.sql`). Not applied to any Supabase project.
**After**: full suite 7007 passed, 4 skipped, 0 failed. UI not exercised in a browser.

---

## Goal

A G1 lab order with its sample collected gets structured parameter values, flags derived
deterministically from age- and sex-specific reference ranges, a pathologist's signature, a letterhead PDF
and WhatsApp delivery. A signed report never changes; a correction is a versioned amendment.

## Flow

```
opd_orders sample_collected ──lis_record_results──▶ lab_results 'entered' (order in_progress)
                         re-save replaces values ◀─┘
'entered' ──lis_validate_result(expected_updated_at)──▶ 'validated' + order 'completed' (one txn)
          └─▶ background: PDF ▶ LabReportService.upload_and_send (external_report_id = lis-<id>)
'validated' + new values + reason ──▶ v2 'entered' (supersedes_id) ──sign──▶ v2 validated, v1 'amended'
```

## Design decisions

| Decision | Why |
|:-|:-|
| Entry and sign-off are SQL functions locking the order row first | Result, values and order status move together; ten concurrent first saves create one result (tested). |
| Sign-off needs `expected_updated_at` | A re-entry between review and signing would otherwise put the pathologist's signature on values they never saw. |
| Validated order goes to `completed`, not `resulted` | `resulted` is an open worklist status: the row would sit on the desk forever. The guard already allows in_progress → completed and the IPD trigger closes the ward order. |
| `lis_guard_result` trigger: signed rows freeze everything except `pdf_storage_path` (once) and validated → amended (only inside the function) | Immutability is a database rule, not an API convention. Deletes only during the owner's clinic purge. |
| Flags computed in `app/services/lis.py`, never accepted from the browser | Zero-LLM, testable pure functions; critical limits checked before normal ones. |
| `not_evaluated` flag | No band covers this patient's age / sex (or unknown sex with only sex-specific bands): the value is reported unflagged rather than wrongly "normal". |
| `lab_results.lab_test_id`, `lab_result_values.parameter_id` are references only; name, unit, range text snapshotted | A signed report outlives catalogue edits and the existing hard delete of a lab test. |
| `letterhead_snapshot`, `patient_snapshot` on the result | The PDF is re-rendered from the row; a reprint is identical to what was signed (same pattern as OPD prescriptions). |
| Composite FKs on `(clinic_id, …)`; `lab_tests` gained `UNIQUE (clinic_id, id)` | A cross-tenant parameter or result is refused by Postgres too. |
| Signer must be a login linked to a doctor with a registration number | The legal signature; PATHOLOGIST logins now require a doctor link like DOCTOR. |
| G1 text completion refused once LIS values exist | Otherwise a text note could close the order and skip the pathologist. |

## Permissions

`LAB_ENTRY` (LAB_OPERATOR, DIAGNOSTIC_OPERATOR, PATHOLOGIST presets) and `LAB_VALIDATE` (PATHOLOGIST, DOCTOR).
Roles imply their grant (`holds_lab_permission`) so logins saved before 118 work. New staff role
`PATHOLOGIST`. Parameter set-up uses the existing `LAB_TESTS_MANAGE`.

## Deviations from the task spec

- PHLEBOTOMIST does not get `LAB_ENTRY`: that login is confined to home collection and the desk worklist
  in `verify_credentials`; sample collectors do not result tests (same rule as G1).
- Added columns: `test_name`, `family_member_id`, `amendment_reason`, `letterhead_snapshot`,
  `patient_snapshot` (results); `is_active`, `updated_at` (parameters); `sort_order` (values);
  flag `not_evaluated`. Dropped FKs to `lab_tests` / `lab_test_parameters` from result rows (see above).
- Added endpoints `GET /admin/lis/results`, `GET /admin/lis/results/{id}`, and parameter list / create /
  update under `/admin/lis/tests/{id}/parameters` — without them no parameter can be defined or queued.
- Delivery goes through `upload_and_send`, which also writes the pipeline's existing AI patient summary
  message. The clinical path (ranges, flags, signature) is LLM-free; the summary is the pipeline's
  existing behaviour for every lab report.

## Files

`migrations/118_lab_information_system.sql`, `migrations/rollback/118_down.sql`, `app/services/lis.py`,
`app/routers/lis.py`, `app/services/opd_pdf.py` (`render_lab_report_pdf`), `app/services/permissions.py`,
`app/tenancy.py`, `app/main.py`, `app/services/orders.py` (worklist `catalog_type`, completion guard),
`app/routers/admin.py` (pathologist doctor link), `admin/index.html` (worklist "Enter results", worksheet,
Lab Sign-off page, review, parameter set-up, role + grants), `tests/test_migration_118_lis.py`,
`tests/test_lis_api_e2e.py`, `tests/test_ipd_no_llm.py`.
