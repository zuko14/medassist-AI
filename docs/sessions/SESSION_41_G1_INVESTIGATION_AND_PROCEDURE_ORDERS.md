# SESSION 41 — G1 Investigation & Procedure Orders

**Date**: 2026-10-28
**Branch**: `hardening/production-readiness-audit` (uncommitted; not deployed)
**Migration**: `115_investigation_and_procedure_orders.sql` (+ rollback). Not applied to any Supabase project.
**Baseline**: lint + migration 114 + OPD billing + IPD E2E green (26 passed). **After**: full suite 6876 passed, 4 skipped, 0 failed.

---

## Goal

Gap analysis G1: a doctor's "CBC, X-ray chest" must reach the bill and the lab desk without being retyped,
for OPD consultations and IPD ward orders, with cancellation safe around money.

## Design decisions (and where they differ from the brief)

| Decision | Why |
|:-|:-|
| `opd_invoice_items.order_id` instead of `opd_orders.invoice_item_id` | `opd_save_invoice_draft` (mig 114) deletes and re-inserts every line on each save; a link stored on the order would dangle after the first save. The redefined draft save carries `order_id` through. |
| IPD orders are **mirrored** into `opd_orders` by a trigger (`source='ipd'`) instead of read from `ipd_orders` by the worklist | `ipd_orders` content is immutable and has no place for barcode / collection fields; one row per order line is the gap-analysis invariant. The trigger also proposes the IPD charge, atomically with the order. |
| Placement, billing and cancellation are SQL functions | Price/sample snapshot, idempotency, bill-line insert and cancel-with-line-removal each happen in one transaction under row locks (appointment → invoice → order: same order everywhere, so no deadlock). |
| Supplementary draft bill | The visit has one live bill (`uq_opd_invoices_one_per_visit`) and the consult fee is usually paid before tests are ordered. Orders placed after the visit bill is issued go on a second draft (appointment_id NULL, encounter_id set). |
| Paid order cannot be cancelled (HTTP 409, the brief's wording) | The money path is refund at the counter → void the bill (orders return to `ordered`) → cancel. Issued-but-unpaid bills are refused too (an issued line is immutable). |
| Permission names `ORDERS_*` (uppercase) | Repo convention for `PERMISSIONS`; the brief's `orders:read` etc. map one to one. No `LAB_TECH` role exists: `LAB_OPERATOR` / `DIAGNOSTIC_OPERATOR` presets carry the lab grants. |
| Pre-115 module grants imply order grants | Existing doctor / nurse / cashier logins work on deploy without re-saving accounts. |
| Placing orders: treating doctor's own login only | Same rule as encounter notes and signing; a clinic admin without a doctor link cannot order. |
| Completing (result) needs `ORDERS_WRITE` | A sample collector (phlebotomist) never records results. Lab presets carry WRITE; placing still needs the doctor link. |
| Collect refuses orders with no specimen (409) | An ECG / X-ray is completed directly. |

## Files

| File | Change |
|:-|:-|
| `migrations/115_investigation_and_procedure_orders.sql`, `migrations/rollback/115_investigation_and_procedure_orders.down.sql` | New |
| `app/tenancy.py` | `opd_orders` tenant-owned |
| `app/services/permissions.py` | `ORDERS_*`, presets, `holds_order_permission` |
| `app/services/orders.py`, `app/routers/orders.py`, `app/main.py` | New service and routes, mounted |
| `app/services/opd_billing.py` | `order_id` through create / draft save; order snapshot price for non-admins; pending orders added when a visit bill opens |
| `app/routers/opd.py` | `InvoiceItemIn.order_id` |
| `app/routers/admin.py` | Phlebotomist allowlist: worklist + collect |
| `app/assistant/registry.py` | `dxworklist` page |
| `admin/index.html` | Consult orders card, billing banner, Diagnostic Worklist page, permission pickers, phlebotomist landing |
| `tests/test_migration_115_orders.py`, `tests/test_orders_api_e2e.py` | New |
| `tests/ipd_pg_bridge.py` | `or=` filter (used by `restrict_to_branch`) |
| `tests/test_ipd_no_llm.py` | Orders modules and `opd_orders` added to the zero-LLM guard |
| `tests/test_migration_103_opd.py` | Rolls back 115 before 111/103 (newest first) |

## Verification

See [`docs/hospital-os/status.md`](../hospital-os/status.md#evidence-2026-10-28-local-pgserver-postgres-python-312) for the
exact commands and results. All five verification groups of the brief pass; the full suite is green.

## Deploy order

1. Apply 115 on a Supabase staging branch; run `pytest tests/test_migration_115_orders.py tests/test_orders_api_e2e.py`
   with `TEST_DATABASE_URL` pointed at it.
2. Apply 115 to production off-peak (additive; pre-115 code ignores the new column and never calls the functions).
3. Deploy the code.
4. Re-save lab-operator staff accounts (or grant `ORDERS_*` explicitly) — see status G1-2.

Rollback: code revert first (115 is additive); the down migration refuses while any order is open and destroys order
history, so take `pg_dump -t opd_orders` before running it.
