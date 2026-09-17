# Session 08 — Platform Financial Control Centre: the Kriya AI books

**Date:** 2026-09-17
**Branch:** main
**Migration:** 079
**Scope:** platform owner panel only (`/platform/*`, `admin/platform.html`)

---

## 1. Intent

The owner dashboard could read what the platform **costs** but had nowhere to
record what it **earns**. Everything financial lived in the owner's head or a
spreadsheet: what each clinic was charged, what Render and Supabase cost,
whether a clinic had actually paid, and whether any of it left a profit.

The ask was a financial CRM inside the panel rather than a fifth external
tool — editable prices (they start low and rise), liabilities, revenue,
per-clinic profit and margin — with zero tolerance for disturbing production.

## 2. The two findings that shaped the work

**`Platform Revenue (30d)` was not the platform's revenue.** `/platform/revenue`
reads `appointments.amount_paise` — patients paying *clinics* through Razorpay.
That money never reaches this business. The tile has been relabelled **Clinic
GMV (30d)** (label text only; the figure and every line of its logic are
untouched). Kriya's own revenue was recorded nowhere, which is the actual gap
this session fills.

**The dashboard's `1,000 outbound / ₹120` was a row cap, not a count.**
`message_accounting.py` selected `outbound_message_ledger` with no `.range()`.
PostgREST caps an unbounded select at 1000 rows and returns the truncation
silently — no error, no flag. The per-clinic split summed to exactly
746 + 202 + 52 = 1,000, which is the cap, not a coincidence.

The same bug sat in the sibling `get_clinic_usage()`, and there it was worse:
that count is what decides whether a clinic has exceeded its message quota, so
a clinic on the 2,500- or 5,000-message tier **could never be seen to go over**.
Both now route through one paginated `scan_outbound_ledger()`.

This was in scope rather than a detour: per-clinic Meta spend is the only
variable cost in the new P&L, so a truncated ledger would have flattered every
margin on the page.

> **Deploy note:** message counts and Meta costs will *rise* after this ships.
> That is the correct number appearing, not a regression.

## 3. Decisions

| Decision | Choice | Why |
| :-- | :-- | :-- |
| Billing basis | Plan default rate, overridable per clinic | Early customers were signed cheap; a later rise in the list price must not silently reprice them. |
| Where the override lives | New `platform_billing_rates` table, **not** a column on `clinics` | `clinics` is on the hot path of every inbound WhatsApp message and is cached per tenant. An owner-only billing concern must not widen that row or change the cache shape. |
| Rate history | Current state only; history lives in the **invoice snapshot** | An invoice freezes `rate_paise`/`locations_billed`/`plan` at generation. Raising a price in November cannot rewrite September's bill. |
| Expense history | Versioned in place — edit **closes** the old row and **inserts** a new one | Past months must keep costing what they cost. Windows are month-aligned so a row and its replacement can never both land in one month and double-count it. |
| Per-clinic profit | Revenue − **that clinic's own Meta spend**, nothing else | Render and Supabase are genuinely shared; any split is an accounting choice, not a fact. Allocating them would make a small clinic look unprofitable for a bill it did not cause. They are subtracted once, platform-wide. |
| Two profit lines | `net_profit` (MRR − expenses) **and** `cash_profit` (collected − expenses) | They differ by exactly what clinics owe but have not paid. Showing only the first is how a business feels profitable while running out of money. |
| Invoice generation | Manual button, idempotent per `(clinic_id, period_month)` | No scheduler until clicking once a month becomes annoying. The unique key is the backstop against a double click. |
| Meta cost as an expense row | **Refused** — computed live from the ledger, never stored | A manual row would double-count against the live figure. |
| Month semantics | IST calendar month (`YYYY-MM`) | Clinics are Indian. A UTC month pushes 5½ hours of every month-end into the wrong month's books. |
| Unpriced plan | Resolves to ₹0, never an invented default | The owner budgets off this number; a guess would inflate MRR. |

## 4. Files changed

**New**

| File | Lines | What |
| :-- | --: | :-- |
| `migrations/079_platform_finance.sql` | 207 | Three tables + RLS + four seeded expense rows at ₹0 |
| `migrations/rollback/079_down.sql` | 55 | Guarded drop — refuses once real data exists |
| `app/services/platform_finance.py` | 509 | Pure money rules + the P&L rollup |
| `tests/test_platform_finance.py` | 578 | 42 tests |

**Modified**

| File | Δ | What |
| :-- | --: | :-- |
| `app/routers/platform.py` | +712 | 11 endpoints under `/platform/finance*` |
| `admin/platform.html` | +711 | Financial Control Centre section, 3 modals, renderers |
| `app/services/message_accounting.py` | +89 / −33 | `scan_outbound_ledger()`; both sweeps rewired onto it |

**Schema (all new, no `ALTER` on any existing table)**

- `platform_billing_rates` — `clinic_id` PK, `rate_paise`, `billing_mode`
  (`per_location` \| `flat`), `notes`. *Absence of a row means "plan default".*
- `platform_expenses` — `name`, `category`, `amount_paise`, `is_recurring`,
  `month` (one-offs), `effective_from`/`effective_to`.
- `platform_invoices` — unique `(clinic_id, period_month)`, snapshot columns,
  `status`, `amount_paid_paise`, `paid_at`.

**Endpoints** (all behind `verify_owner_credentials`)

```
GET    /platform/finance?month=YYYY-MM        the P&L rollup
GET    /platform/finance/rates
PUT    /platform/finance/rates/{clinic_id}    set a negotiated rate
DELETE /platform/finance/rates/{clinic_id}    revert to plan default
GET    /platform/finance/expenses
POST   /platform/finance/expenses
PUT    /platform/finance/expenses/{id}        versions on an amount change
DELETE /platform/finance/expenses/{id}        closes if it has history
GET    /platform/finance/invoices
POST   /platform/finance/invoices/generate    idempotent
PATCH  /platform/finance/invoices/{id}        record payment
```

## 5. Tests

`tests/test_platform_finance.py` — **42 passed**. The money rules are pure
functions, so most of the file needs no database.

Guards worth naming:

- `test_override_survives_a_plan_price_rise` — the whole reason the override is
  a row and not a recomputation.
- `test_superseded_expense_and_its_replacement_never_share_a_month` — the
  double-count guard. Render at ₹2,100 → ₹2,500 in September: August must
  return only the old row, September only the new.
- `test_ledger_scan_pages_past_the_postgrest_row_cap` — 2,350 rows must come
  back as 2,350, not 1,000.
- `test_invoice_generation_is_idempotent_and_skips_inactive_clinics` — two
  clicks must not double-bill; a churned clinic must not be billed at all.
- `test_partial_payment_covering_the_full_invoice_is_refused` — otherwise it
  reads as outstanding forever.
- `test_messaging_usage_payload_shape_is_unchanged` — regression fence on the
  endpoint that was rewired onto the new scanner.

**Regression sweep — 151 passed** across `test_platform_finance`,
`test_message_accounting`, `test_lint_unscoped_queries`,
`test_no_blocking_db_calls`, `test_billing_api_separation`,
`test_platform_roster_and_plan_features`, `test_outbound_audit_feed`,
`test_multispecialty_plan`.

The full suite was **not** run: `tests/test_multi_worker_smoke.py` boots real
uvicorn workers against the production Supabase and leaves orphans holding
production scheduler locks. See `[[local-app-runs-steal-prod-locks]]`. Process
table checked after every run — clean.

Frontend verified separately: `node --check` on the extracted script body, plus
a scan asserting every `getElementById` has a matching `id`, every inline
handler names a defined function, and `<div>` tags balance.

## 6. Go-live runbook

1. Apply `migrations/079_platform_finance.sql` in Supabase. **Until then the
   panel shows "Financial tables not found" and nothing else on the dashboard
   is affected** — the loader mirrors `loadPricingConfig()`'s try/catch and the
   endpoints return 503, not 500.
2. Set plan prices — every tier is currently ₹0, so MRR reads ₹0 until they are
   set. Either per-plan (`PUT /platform/plan-tiers/{plan}`, already existed and
   was always display-only) or per-clinic via the **Rate** button.
3. Fill in the four seeded expense rows — Render, Supabase, AI API, Domain &
   DNS. They are seeded at ₹0 deliberately: a made-up figure would be worse
   than a blank one.
4. Click **Generate Invoices** once a month, then record payments as they land.

## 7. Open items

- **No cron auto-invoicing.** Deliberate. Add a scheduler job when clicking a
  button once a month becomes annoying — the endpoint is already idempotent, so
  a job would just call it.
- **No GST / tax fields, no PDF invoices, no multi-currency.** Not asked for.
- **`billing_mode = 'flat'`** is implemented and tested but unused so far; it
  exists because a negotiated all-in deal is a real shape and discovering you
  cannot model your own contract is worse than one extra column.
- **Expense frequency is fixed once a row exists** (the UI hides the control on
  edit). Flipping a recurring cost to a one-off would orphan the months it had
  already applied to.
- `get_outbound_audit_feed()` still selects the ledger directly, but it is
  bounded by an explicit `limit` and is a feed, not a total — left alone.
