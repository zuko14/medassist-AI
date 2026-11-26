# Kriya Revenue Guard (RI) — Execution Status

> Spec: [SPEC.md](SPEC.md). Same states and rules as [docs/continuous-care/status.md](../continuous-care/status.md)
> (never VERIFIED without the pasted gate summary; update this file in the same commit).

## 1. Current pointer
| Field | Value |
|:-|:-|
| Active phase | RI-00…RI-08 built (2026-11-25); VERIFIED in the test suite; not yet applied to Supabase — owner enables per clinic |
| Next task | Owner: apply migration 143 on Supabase after 125–142, then enable `revenue_guard` for a pilot clinic |
| Migration | **143** `143_revenue_guard.sql` + `rollback/143_rollback_revenue_guard.sql` (renumbered from the provisional 148) |
| Parallel with | CC P1–P3 (no shared files except `tenant.py`, `permissions.py`, `tenancy.py`, `scheduler.py` — merge carefully) |

## 2. Tasks
| ID | Task | Depends | Acceptance | Tests | State |
|:-|:-|:-|:-|:-|:-|
| RI-00 | Baseline gates on `main`; re-verify SPEC §2 evidence by symbol | — | green | CG1–CG7 | VERIFIED — baseline 10,276 passed / 6 skipped before any change; SPEC §2 symbols re-checked (`_billing_by_order`, `bill_pending_orders`, ipd_charges / ot_surgeries.charge_id / dx_b2b_ledger.statement_id, `_apply_prepaid`) |
| RI-01 | Migration 148 + rollback + verify block; tenancy registration | RI-00 | idempotent; no trigger on existing tables | `test_migration_ri.py` | VERIFIED — `ri_runs`, `ri_findings` (+ `clinics.ri_settings`), RLS forced, registered in `TENANT_OWNED_TABLES`; no trigger; idempotent + rollback/re-apply (`test_migration_ri.py`) |
| RI-02 | `revenue_guard` opt-in, permissions, `/admin/me` keys, owner toggle | RI-01 | OFF by default; wildcard never enables | `test_ri_feature_gate.py` | VERIFIED — `OPT_IN_FEATURES['revenue_guard']`, `ri_access` / `ri_eligible` / `ri_modules` (needs OPD/IPD/DX/RX), `RI_VIEW`/`RI_RESOLVE` (role-implied for CASHIER / BRANCH_MANAGER without touching `role_implied_grants`), `/admin/me.ri_access/ri_perms`, owner toggle with eligibility (`test_ri_feature_gate.py`) |
| RI-03 | Rules OPD (`opd_seen_not_billed`, `prepaid_not_invoiced`, billing-adoption gate, `order_done_not_billed`, `order_stale_unbilled`, `draft_bill_stale`) | RI-01 | SPEC §3 | `test_ri_rules.py` | VERIFIED — `ri_scan` OPD rules incl. billing-adoption gate (default 20 bills / 30 d), prepaid → review (D12), free-review / discount-reason exemptions recorded once (`test_ri_rules.py`) |
| RI-04 | Rules IPD/OT/RX/DX | RI-03 | SPEC §3; integrity rules raise admin alert | `test_ri_rules.py` | VERIFIED — IPD unposted / discharged-unbilled / discharge-pending, OT done without charge, RX dispense without charge (integrity → admin notification), RX signed-not-sold (opportunity, 7-day window), DX B2B off-statement |
| RI-05 | Nightly job + auto-resolve + recovered ₹ + 08:00 digest | RI-03 | SPEC §5 | job tests | VERIFIED — `ri_nightly_scan` 01:30 IST + `ri_digest` 08:00 IST (CAS-locked), auto-resolve billed / voided / auto_closed with recovered ₹ (prepaid recovers 0), WhatsApp digest via the clinic's own credentials |
| RI-06 | API + route matrix | RI-05 | SPEC §7 | `test_ri_route_matrix.py` | VERIFIED — `/admin/ri/summary|findings|findings/{id}/dismiss|run|settings`; pinned in `test_ri_route_matrix.py` and both global matrices; branch pin; manual re-run capped 3/day |
| RI-07 | UI page + dashboard tile + owner console figure + Ketu `ri.yesterday` | RI-06 | UI-G1…G12 (UI spec §8) | manual + `test_assistant_registry.py` | VERIFIED (automated) — `riguard` page (`RI:` blocks, tokens only), main-dashboard tile, owner-console value line (X14 dismissed ₹), Ketu `ri.yesterday` (R0, corpus + pinned reads). UI-G2 screenshots pending a live pilot |
| RI-08 | Zero-impact + release readiness | all | SPEC §8 | `test_ri_zero_impact.py` | VERIFIED — `test_ri_zero_impact.py`: refusal before any ri_* read, no ri_* query on existing routes (module on or off), sweep/digest skip, read-only SQL, no trigger |

## 3. Log
| Date | Entry |
|:-|:-|
| 2026-11-24 | Spec written from code audit at `c8c37ff`; no code changed |
| 2026-11-25 | RI-00…RI-08 built. Shortcuts recorded in code: `rx_signed_not_sold` counts only prescriptions with no completed sale (partial fills stay on the RX lost-sale list) and carries ₹0 (no reliable Rx-line → MRP map yet); no recovered-₹ trend chart (tiles only, so no `toggleTheme()` line). Existing test lists appended (not weakened): `tests/test_assistant_reads.py` READS / CASES / handler set and the corpus gained `ri.yesterday`. Gates: see the final gate block in docs/KRIYA_NEXT_ROADMAP.md |
