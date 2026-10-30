# SESSION 51 — Pre-production re-audit of sessions 40–50 and Kriya Ketu voice

**Date**: 2026-10-30
**Branch**: `hardening/production-readiness-audit` (uncommitted; not deployed)
**Scope**: every uncommitted change on the branch: migrations 115–124, 8 new routers and 10 new services,
the edits to live OPD/IPD billing, permissions and tenancy, the admin panel (+5,000 lines), and the Kriya Ketu
voice assistant (OpenRouter STT, spoken-lead stripping, hospital-module actions).
**Production checked read-only** (Supabase project `Kriya AI`, metadata and counts only; no patient rows read).

---

## Verdict

Safe to ship **after** the two fixes below and **only in this order**: apply 115–124 to Supabase, then merge to
`main`. One high-severity privilege escalation was found and fixed; nothing else blocks.

> **Deploy order is mandatory.** `main` auto-deploys. The new code reads `opd_credit_notes` (117),
> `ipd_deposits` (116), `opd_service_catalog()` (120) and calls `opd_bill_orders()` (115) on every OPD bill,
> invoice view and cashier-shift view. Deployed before the migrations, OPD billing fails for the live client.
> The reverse is safe: the currently deployed code runs unchanged on the 124 schema (new columns default,
> new triggers act only on new modes / IPD bills).

## Defects fixed

| # | Severity | Defect | Fix | Test |
|:-|:-|:-|:-|:-|
| 1 | **High** (privilege escalation) | The new `holds_*_permission` helpers grant authority from `staff_role` alone (CASHIER → credit notes with cash refund; TPA_DESK → insurance manage; DOCTOR / PATHOLOGIST / RADIOLOGIST → OT, LIS sign-off, radiology). `cap_permissions_to_authority` caps only stored grants, so a non-admin with `STAFF_CREATE` could mint any of these by picking the role, and a `STAFF_UPDATE` holder could promote **their own** account. Reachable in production: one BRANCH_MANAGER holds STAFF_CREATE/UPDATE, one STAFF holds STAFF_UPDATE. | `permissions.role_implied_grants` + `role_grants_beyond_authority`; `create_staff` / `update_staff` refuse (403) a role whose implied grants the caller does not hold. Admins unaffected; an unchanged role on edit is not re-checked. PHLEBOTOMIST is left out (confined login). Live impact: none — every staff create / update in the production audit log (30) was by a clinic admin or super-admin. | `tests/test_admin_staff_accounts.py` (4 cases) |
| 2 | Medium (GST) | `ot_complete_surgery`: an entered amount skipped the linked service lookup, so a taxable procedure (e.g. cosmetic, 18 %) posted with 0 % GST and no SAC. | Migration 122 (not yet applied anywhere): the service's rate and SAC always apply; the amount replaces only the price. | `test_entered_amount_keeps_the_linked_services_gst` |
| 3 | Cleanup | `import asyncio` left unused in `opd.py` / `opd_billing.py` after the `spawn_background_task` change. | Removed. | — |

## Verified (no change needed)

| Area | Method | Result |
|:-|:-|:-|
| Supabase API exposure | Fresh Postgres with Supabase's default grants (`ALTER DEFAULT PRIVILEGES … TO anon, authenticated`), all 124 migrations applied, then `has_table_privilege` / `has_function_privilege` for `anon` and `authenticated` | All 28 new tables: RLS enabled + forced, API roles revoked. Every new RPC revoked from API roles. No `SECURITY DEFINER`. Every function has a pinned `search_path`. |
| Production state | `schema_migrations`, security advisor, aggregate counts | At 114; advisor clean; live data satisfies every new constraint (4 invoices, cash only, 0 admissions, no GSTIN or legacy catalogue) |
| New endpoints (~97) | AST scan of every route + reading each guard helper | All authenticated; permission, tenant and branch checked (directly or via `_admission` / `_visit` / `_surgery`); writes audited; no raw DB text in responses |
| Admin panel | Every new `innerHTML` interpolation and inline handler | Data escaped with `esc()`; inline handlers interpolate only ids and constants; user URLs pattern-validated `https?://`; dialogs use `textContent` |
| Patient identity merge | Read `emergency_merge_patient` + later FKs | Surgeries, radiology, Form F, pre-auth follow via `ON UPDATE CASCADE` composite FKs |
| Kriya Ketu voice | Read STT + router + registry diffs | Body capped at 1 MiB (30 s WAV fits), 60/h per user, spend cap checked, audio never stored, transcript never logged; new actions are reads or hand-offs (nothing executable); the dispatched route re-checks permission |
| Tests | `pytest tests/` before and after | before **7667 passed, 4 skipped, 0 failed**; after **7672 passed, 4 skipped, 0 failed** (5 new) |

## Open items (owner decisions, not defects)

| # | Item | Recommendation |
|:-|:-|:-|
| R-1 | Staging dry run of 115–124 on a Supabase branch (the G*-1 items) has not been done | Do it before production; it costs a branch for the duration |
| R-2 | `ASSISTANT_STT_PROVIDER=openrouter` sends staff speech (may include patient names) to OpenRouter / Google. Default stays Sarvam | Before switching: DPDP processor review; consider `provider.data_collection: "deny"` in the payload. Its cost is logged as ₹0, so the AI spend cap does not count it |
| R-3 | Any `OT_CLINICAL` doctor may override the tariff price when finishing a surgery; the charge posts directly | Decide whether an override needs `IPD_BILLING` / `IPD_ADMIN` (OPD already refuses non-admin re-pricing) |
| R-4 | PHLEBOTOMIST's implied `ORDERS_READ` / `ORDERS_COLLECT` is not capped (fix #1 leaves it out) | Low: the login is confined to two routes |
| R-5 | Helper SQL functions (`*_in_tx`, `*_purging`, `ipd_log`, …) stay executable by `anon` | Low: SECURITY INVOKER with no table privileges, so they do nothing; revoke in a later migration for hygiene |
| R-6 | LIS / radiology reports auto-send through the existing lab-report pipeline, which does not check `opted_in`; clinical documents do | Same as today's lab reports; align if the owner wants consent on reports too |

## Production rollout

1. Supabase staging branch: apply 115–124, run `TEST_DATABASE_URL=… pytest tests/test_migration_1*.py tests/*_e2e.py`.
2. Production: apply 115–124 in order (`scripts/migrate.py`), confirm `schema_migrations` shows 124.
3. Merge the branch to `main` (auto-deploy).
4. Re-save or explicitly grant `ORDERS_*` to lab / diagnostic operators created before 115 (G1-2).
