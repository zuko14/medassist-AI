# SESSION 46 — G12 Central Masters, Service Catalogue & ICD-10

**Date**: 2026-10-28
**Branch**: `hardening/production-readiness-audit` (uncommitted; not deployed)
**Migration**: `120_clinical_coding_and_masters.sql` (+ `rollback/120_down.sql`). Not applied to any Supabase project.
**After**: full suite 7080 passed, 4 skipped, 0 failed. UI checked at 375 px against stubbed data, not a live backend.

---

## Goal

A relational service / tariff master replacing the JSON catalogue in `clinics.opd_settings`, without
breaking any clinic, and deterministic ICD-10 diagnosis autocomplete in OPD and IPD.

## Design decisions

| Decision | Why |
|:-|:-|
| One lookup in SQL: `opd_catalog_item(clinic, code)` (master row wins, legacy JSON entry as fallback) and `opd_service_catalog(clinic)` (master + legacy codes not yet in it) | The catalogue was read in five places, two of them SQL (`opd_place_orders`, `opd_invoice_item_tax`). Python-only fail-open would leave orders and GST reading the JSON. Python billing, orders catalogue and voice call the same function. |
| 115 / 117 functions copied verbatim and only the lookup lines patched (generated, not retyped); rollback restores the originals verbatim | No silent drift in order placement or tax. |
| Backfill = `opd_import_service_catalog(clinic, items, overwrite=false)`, run for every clinic in the migration; idempotent (`ON CONFLICT DO NOTHING`); malformed items get safe defaults instead of aborting the migration | Re-runnable; the JSON is left in place for rollback and pre-120 code. |
| The legacy `PUT /admin/opd/setup/settings {service_catalog}` writes through (`overwrite=true`) | Existing scripts / onboarding keep working and actually change billing. It upserts; it never deactivates master rows missing from the list. |
| `service_code` immutable (trigger); `DELETE` hard-deletes only a never-used service, otherwise deactivates | Bills and orders carry the code. |
| Category adds `diagnostic` to the spec's list; tax slabs include 40% | Legacy items were `diagnostic` (ECG, X-ray ordered as investigations); 117's GST slabs allow 40%. |
| Branch: `NULL` = all branches; a branch-pinned manager creates for and edits only their branch | Code is unique per clinic, so pricing lookups ignore branch; branch is availability / ownership. |
| `SERVICES_MANAGE` in no preset; `OPD_ADMIN` implies it | OPD admins edited the old catalogue. |
| ICD-10: `icd10_search()` SQL function; code prefix → description prefix → substring → (pg_trgm) word similarity; short queries prefer `is_common` | One indexed call, deterministic, zero-LLM. LIKE wildcards in the query are escaped. |
| `pg_trgm` + GIN index created only when the server has the extension (Supabase does); otherwise `text_pattern_ops` prefix indexes | The test Postgres has no pg_trgm. **The fuzzy (typo) tier and GIN index were not exercised in tests**; prefix / substring paths were. |
| 467 WHO ICD-10 codes seeded across all 22 chapters (chapter derived from the code range, U = XXII); everyday terms in brackets ("[piles]", "[type 2]") | Searchable out of the box; the full 12k-code WHO list can be loaded later into the same table. |
| OPD `Diagnosis` code regex now admits U-codes | `U07.1` (COVID-19) was rejected. |

## UI

- **Service Catalogue** page (OPD nav, admin + billing): search, category filter, show inactive, Code /
  Name / Category / Price / HSN/SAC / GST / Active toggle, "+ Add New Service" modal, Edit, Delete,
  "Move to master" for legacy rows. Cards on narrow screens; no horizontal scroll at 375 px (checked).
- **ICD-10 typeahead**: one accessible combobox (↑ ↓ Enter Esc, `aria-activedescendant`) on the OPD
  diagnosis rows (fills code + text → stored as `{"system": "ICD-10", "code", "text"}`), IPD provisional
  diagnosis, and the discharge summary final diagnosis (one per line). Delegated listeners, so rows added
  later work. Opaque list placed below / above the field, clamped to the viewport (checked at 375 px).
- Staff forms offer `SERVICES_MANAGE`.

## Deviations from the task spec

`service_code` pattern `^[A-Z0-9_-]{2,40}$` (what orders and bill lines accept); `branch_id` composite FK;
`updated_at` touch trigger; delete is soft when the service is in use; seed is 467 codes (≥ 300).

## Files

`migrations/120_clinical_coding_and_masters.sql`, `migrations/rollback/120_down.sql`,
`app/services/masters.py`, `app/routers/masters.py`, `app/services/opd_billing.py` (catalogue reads),
`app/routers/opd.py` (settings write-through, U-codes), `app/voice/knowledge.py`,
`app/services/permissions.py`, `app/tenancy.py`, `app/main.py`, `app/assistant/registry.py`,
`admin/index.html`, `tests/test_migration_120_masters.py`, `tests/test_masters_api_e2e.py`,
`tests/test_opd_billing.py` (mocks the new catalogue boundary), `tests/test_migration_103_opd.py`,
`tests/test_ipd_no_llm.py`.
