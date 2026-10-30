# SESSION 42 — G5 IPD Finance

**Date**: 2026-10-28
**Branch**: `hardening/production-readiness-audit` (uncommitted; not deployed)
**Migration**: `116_ipd_finance.sql` (+ `rollback/116_down.sql`). Not applied to any Supabase project.
**Before**: full suite 6876 passed (after Session 41). **After**: 6936 passed, 4 skipped, 0 failed.

---

## Goal

Gap analysis G5: a nursing home must run inpatient money in Kriya — advances, a bill the family can see
mid-stay, package pricing, controlled discounts and corporate credit — without a side ledger.
Spec written first: [`docs/hospital-os/G5-IPD-FINANCE-SPEC.md`](../hospital-os/G5-IPD-FINANCE-SPEC.md).

## Design decisions

| Decision | Why |
|:-|:-|
| Deposits are their own append-only ledger (`ipd_deposits`), not receipts | `opd_receipts` needs an invoice; a deposit is taken days before the final bill exists. Applying it later writes an ordinary receipt (mode `deposit`) so the bill's paid amount still moves only through the receipt trigger. |
| Cash deposits count in the cashier's shift | They are physically in the drawer: `opd_close_shift` and the live shift view add deposit cash in, deposit refunds out. Day collections show deposits separately and skip deposit-mode receipts (counted once, when taken). |
| Balance check in the ledger's insert trigger under the admission row lock | Two concurrent refunds of the same balance: the second waits, sees the first, and is refused (tested with parallel connections). |
| Interim bill = statement, not an invoice | The stay keeps exactly one final bill (`uq_opd_invoices_one_per_admission`); a second invoice per stay would break receipts, voids and the discharge checklist. Mid-stay money is collected as deposits. |
| `ipd_bill_lines()` shared by final and running bill | Preview and bill cannot disagree on package cover. |
| Package cover rule | Included categories within `max_days` of admission are covered (one ₹0 summary line with the tariff value); excluded categories and later days are billed. Snapshot at application. |
| Discount authority as settings, four-eyes in the database | Gap analysis §9 left limits to the owner; defaults are conservative (staff 0). `requester ≠ approver` is a CHECK, and the bill can be issued only with exactly the approved amount. |
| Credit = authorisation, not a waiver | The settlement waiver records an exception; credit records who pays, how much, and the PO. The bill stays open as a receivable; `bank_transfer` receipts settle it. |
| Void of a deposit-paid bill returns the deposits first | Voids require `paid = 0`; returning the deposit to the stay keeps the money traceable. Only when deposits are the bill's only payments — otherwise the normal refund-first refusal stands, with nothing moved. |

## Defects found and fixed along the way

| Defect | Fix |
|:-|:-|
| **Pre-existing (since 103)**: the owner's clinic purge failed for any clinic with a discounted draft bill (deleting its lines recalculated the subtotal below the discount). | `opd_recalc_invoice_subtotal` skips work during a purge (in 116; the rollback keeps it). |
| `tests/test_migration_103_opd.py::test_103_idempotency_and_indexdef` re-applied 103 **committed**, silently replacing newer function definitions in the shared test database for every later test. | Runs inside a rolled-back transaction. |
| The test bridge returned a single object for set-returning RPCs (PostgREST returns an array) — shift close had never been exercised through it. | `tests/ipd_pg_bridge.py` returns arrays for `SETOF` functions. |

## Files

| File | Change |
|:-|:-|
| `migrations/116_ipd_finance.sql`, `migrations/rollback/116_down.sql` | New (rollback restores the 103/111 function bodies verbatim) |
| `app/services/ipd_finance.py` | New service |
| `app/routers/ipd.py` | Finance routes, models, discount settings |
| `app/services/ipd.py` | Error codes, settings defaults |
| `app/services/opd_billing.py` | `resolve_counter_shift` (extracted), deposits applied on IPD bill issue, returned on void, `bank_transfer`, deposits in shift view and collections |
| `app/routers/opd.py` | `ReceiptIn.mode` + `bank_transfer` |
| `app/tenancy.py` | Five new tenant tables |
| `app/assistant/registry.py` | `ipdfinance` page |
| `admin/index.html` | Chart finance panel, interim statement, IPD Setup (discount authority, packages), IPD Receivables page |
| `tests/test_migration_116_ipd_finance.py`, `tests/test_ipd_finance_api_e2e.py` | New |
| `tests/ipd_pg_bridge.py`, `tests/test_migration_103_opd.py`, `tests/test_ipd_no_llm.py` | See above |

## Verification

Commands and results: [`docs/hospital-os/status.md` → G5 evidence](../hospital-os/status.md#g5--ipd-finance).

## Deploy order

1. Supabase staging branch: apply 115 then 116; run `pytest tests/test_migration_116_ipd_finance.py tests/test_ipd_finance_api_e2e.py` with `TEST_DATABASE_URL`.
2. Production off-peak: apply 116 (additive; pre-116 code is unaffected — the redefined functions behave identically for stays without finance records).
3. Deploy code. 4. Owner sets discount limits and packages in IPD Setup.

Rollback: code revert first. `116_down.sql` refuses while any finance record or new-mode receipt exists.
