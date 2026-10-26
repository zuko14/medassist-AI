# Kriya IPD OS — Execution Status (source of truth)

> Read first: [ARCHITECTURE.md](ARCHITECTURE.md) · [ADR.md](ADR.md) · [DATA-MODEL.md](DATA-MODEL.md) ·
> [WORKFLOWS.md](WORKFLOWS.md) · [SECURITY.md](SECURITY.md) · [TEST-PLAN.md](TEST-PLAN.md) · [ROLLOUT.md](ROLLOUT.md) ·
> [KRIYA-KETU.md](KRIYA-KETU.md) · [COMPETITIVE-ANALYSIS.md](COMPETITIVE-ANALYSIS.md)
>
> States: `NOT_STARTED` · `IN_PROGRESS` · `BLOCKED` · `IMPLEMENTED` (code written, evidence incomplete) ·
> `VERIFIED` (the listed command was run and passed — result pasted) · `DEFERRED` (reason given).
> Rule: never mark VERIFIED without pasting the command result. Update this file in the same change as the work.

## 0. Mission and scope
Build IPD OS as an optional, owner-enabled, tenant-isolated extension of OPD OS: wards/beds, admissions,
transfers, nursing station, doctor rounds and orders, MAR, charges → final bill (shared OPD ledger), discharge,
dashboards, owner enable/wind-down, Kriya Ketu reads and hand-offs. OPD behaviour must not change.

## 1. Discovery and baseline — VERIFIED (2026-10-15)
| Item | Result |
|---|---|
| Repo / branch | `mediassist-ai`, `main` @ `d262d1b`, working tree clean at start |
| Instructions read | `CLAUDE.md`, `AGENTS.md`, `docs/opd-os/{SPEC,status}.md`, `docs/kriya-ketu/{architecture,status}.md`, `docs/agent-context/*` index |
| Latest migration before work | `110_config_atomic_apply.sql` |
| Python / test runner | `.venv/Scripts/python.exe` (pytest 8.0.0); system `python` has no pytest |
| **Baseline full suite** | `.venv/Scripts/python.exe -m pytest -q` → **6510 passed, 5 skipped, 0 failed in 333.20s** |
| Pre-existing failures | none at baseline |
| OPD entitlement path traced | `OPT_IN_FEATURES` → `PATCH /platform/clinics/{id}/features` → `clinics.features` + `opd_state` → `opd_enabled()` → `_opd_scope()` on every `/admin/opd` route → `/admin/me.opd_enabled` → `applyOpdGates()` |
| Competitive research | [COMPETITIVE-ANALYSIS.md](COMPETITIVE-ANALYSIS.md) (public pages, 2026-10-15; "Crio" and "Soflam" not identified) |

## 2. Workstreams
| Slice | Scope | State |
|---|---|---|
| S1 Foundation | entitlement, permissions, tenancy, migration, platform toggle, wind-down | VERIFIED |
| S2 Hospital structure | wards, beds, bed lifecycle, concurrency-safe allocation, bed board | VERIFIED |
| S3 Admission lifecycle | registry linkage, admit, reserve, assign, cancel, history | VERIFIED |
| S4 Inpatient workbench | chart, observations, notes, worklist | VERIFIED |
| S5 Care coordination | doctor notes, orders, MAR, transfers | VERIFIED (pharmacy/LIS integration DEFERRED) |
| S6 Billing and discharge | charges, bed-day accrual, final bill via OPD ledger, checklist, summary signing, completion | VERIFIED (deposits/packages/TPA DEFERRED) |
| S7 AI orchestration (Kriya Ketu) | IPD pages, 4 R0 reads, 3 H hand-offs, corpus | VERIFIED (writes DEFERRED, ADR-IPD-08) |
| S8 Hardening and release | matrices, no-LLM guard, full regression, live UI check, docs | IMPLEMENTED (staging run outstanding) |

## 3. Tasks
| ID | Task | Depends | Acceptance | Tests | State |
|---|---|---|---|---|---|
| IPD-01 | Migration 111 + rollback | — | applies on 110; idempotent; verify block; down refuses with active stays; re-apply | `test_migration_111_ipd.py` | VERIFIED |
| IPD-02 | Bed allocation invariants under concurrency | 01 | 20 parallel admits → 1; 6 parallel transfers → 1; no deadlock | same | VERIFIED |
| IPD-03 | Clinical immutability + provenance | 01 | observation/MAR/notes/orders/summary guards | same | VERIFIED |
| IPD-04 | Charges, bed-day accrual, final bill | 01 | idempotent; missing tariff reported; bill = posted charges; OPD cannot edit lines; void → rebill | same + E2E | VERIFIED |
| IPD-05 | Discharge checklist + completion | 01,04 | blocked until complete; waiver only by IPD admin; bed → cleaning | same + E2E | VERIFIED |
| IPD-06 | Entitlement: `OPT_IN_FEATURES.ipd_enabled`, `ipd_access`, `ipd_eligible`, `/admin/me.ipd_access` | — | off by default; needs OPD; enterprise wildcard ignored | E2E | VERIFIED |
| IPD-07 | Permissions + NURSE role (+ nav confinement) | — | 5 permissions; presets at creation only | E2E, live UI | VERIFIED |
| IPD-08 | `app/services/ipd.py` + `app/routers/ipd.py` (38 routes) | 01,06,07 | every route via `_ipd_scope`; doctor-link rule; audit on writes + clinical reads; fixed 500 text | E2E, matrices | VERIFIED |
| IPD-09 | Platform: enable / wind-down / re-enable; OPD-off guard; clinic purge | 06 | 409 + preview with patients; WIND_DOWN keeps care; nothing deleted | E2E | VERIFIED |
| IPD-10 | Tenancy registration (11 tables) + linter | 01 | AST linter green | `test_lint_unscoped_queries.py` | VERIFIED |
| IPD-11 | Security matrices cover `/admin/ipd/*` | 08 | ≥ 38 IPD routes enumerated; cross-tenant + unscoped super-admin refused | matrices | VERIFIED |
| IPD-12 | Zero-LLM guard | 08 | no AI/voice/assistant imports; no IPD tables named in AI/voice/chat/assistant | `test_ipd_no_llm.py` | VERIFIED |
| IPD-13 | Panel UI: Bed Board, Admissions, Inpatient Chart, IPD Setup; staff role/permissions | 08 | live journey with 5 real logins; 390 px no horizontal scroll | live browser run (§4) | VERIFIED (manual) |
| IPD-14 | Owner console IPD card | 09 | enable; 409 → confirm → wind-down | inline script parses; not browser-driven | IMPLEMENTED |
| IPD-15 | Kriya Ketu integration | 08 | RT-14, drift, handler perms, corpus routing, change-request guard | Ketu suites | VERIFIED |
| IPD-16 | Documentation set | — | this folder + AGENTS.md + Ketu status entry | — | VERIFIED |
| IPD-17 | Staging: apply 111 on a Supabase branch; E2E with `TEST_DATABASE_URL` | 01–15 | green on real PostgREST | ROLLOUT §1 | NOT_STARTED |
| IPD-18 | Browser-automated UI regression for IPD pages | 13 | Playwright/agent-browser test in suite | — | NOT_STARTED |
| IPD-19 | `docs/agent-context/03,04,05,08,13` IPD sections | 16 | tables, routes, nav, permissions, gaps | — | NOT_STARTED |
| IPD-20 | Discharge-summary PDF (fpdf2, like OPD Rx) | 05 | printable signed snapshot | — | DEFERRED (browser print view from the signed version exists) |
| IPD-21 | Deposits / advances, packages, TPA/insurance claims, GST | 04 | needs ledger design + owner policy | — | DEFERRED (ADR-IPD-06) |
| IPD-22 | Pharmacy dispensing + stock; LIS order transmission + results | — | needs a system of record | — | DEFERRED (ADR-IPD-05) |
| IPD-23 | Attendant WhatsApp updates | — | approved templates + consent | — | DEFERRED |
| IPD-24 | Clinical early-warning rules | — | clinician-approved rules (O-5) | — | DEFERRED |
| IPD-25 | Ketu IPD writes KK-IPD-1…5 | 15 | KK-P0-15 hardening per endpoint | — | DEFERRED |
| IPD-26 | ABDM discharge summary (FHIR) | 05 | — | — | DEFERRED |

## 4. Verification evidence (commands actually run, 2026-10-15)
| Gate | Command | Result |
|---|---|---|
| Migration / invariants | `.venv/Scripts/python.exe -m pytest tests/test_migration_111_ipd.py -q` | **35 passed in 8.90s** |
| E2E through HTTP + real Postgres | `… tests/test_ipd_api_e2e.py -q` | **10 passed** (incl. branch-scope regression) |
| OPD + IPD migrations + E2E together (after OPD rollback-order fix) | `… tests/test_migration_103_opd.py tests/test_migration_111_ipd.py tests/test_ipd_api_e2e.py -q` | **58 passed in 33.08s** |
| No-LLM guard | `… tests/test_ipd_no_llm.py -q` | **2 passed** |
| Linter + matrices + platform/plan + OPD gate | `… test_opd_feature_gate test_plan_features test_platform test_platform_roster_and_plan_features test_corporate_partner_accounts test_lint_unscoped_queries test_phase4_scoped_queries test_admin_super_admin_scope_matrix test_phase2_route_adversarial_matrix -q` | **902 passed in 68.09s** (before the IPD coverage assertions were added) |
| Kriya Ketu | `… tests/test_assistant_reads.py tests/test_assistant_handoff.py tests/test_assistant_registry.py tests/test_assistant_rules.py -q` | **744 passed in 38.50s** |
| **Full suite (final, all changes incl. branch-scope fix)** | `.venv/Scripts/python.exe -m pytest -q -rs` | **6725 passed, 0 failed, 8 skipped in 418.15s**. Skips: 5 as at baseline (3 live-production, 1 MocDoc sandbox, 1 missing local CSV) + 3 clock-dependent (`test_lab_booking_production_fixes.py:343`, `test_session20_payment_audit_fixes.py:208,217` skip when a 2-hour slot crosses midnight; run started 23:3x IST). None IPD-related. |
| Live UI (manual, real app, real bcrypt logins, embedded PG, no network) | scratch launcher: `uvicorn app.main:app` with `lifespan="off"` + `tests/ipd_pg_bridge.py` | admin: setup 2 wards / 9 beds; desk: registry search → admit `IPD-2026-00001` into GW1-1; nurse: confined nav (Bed Board, Chart), vitals + nursing note, no billing tab; doctor: progress note, medication order, MAR, stop order, start discharge, summary sign; cashier: bed-day + manual charge, post, final bill → OPD `INV-2026-00001` ₹2,100 = Σ charges, no clinical data; admin: unpaid completion refused (409), waiver → discharged, bed → cleaning; timeline attributed per user. 390 px: no horizontal scroll (fixed KPI stacking). Console errors seen only on OPD pages from bridge gaps (`not.is.null`, embedded selects), not app errors. |

## 5. Known issues and limitations
- **Not applied to staging or production.** The E2E bridge emulates PostgREST; a staging run (IPD-17) is required.
- Owner console IPD card not browser-driven (IPD-14).
- `clinic_admin` can read clinical charts (house convention, owner decision O-3); reads are audit-logged.
- Bed board loads a clinic's whole bed set (documented `shortcut:` in `ipd.bed_board`) — fine to a few hundred beds.
- OPD Billing page edits on an IPD bill's lines are refused by a DB trigger with a generic error in that page.
- Open owner decisions O-1…O-5 (ADR.md) have defaults in force; pilot hospital must accept them.
- During this work: OPD's `test_103_down_rollback_and_reapply` needed the newest-first rollback order (111 before
  103) and now runs inside a rolled-back transaction instead of dropping the shared test DB's OPD tables.

## 6. Log
| Date (IST) | Entry |
|---|---|
| 2026-10-15 | Discovery + baseline (6510 / 0 / 5). Competitive research. Migration 111 + rollback; 35 real-PG tests incl. races. Service + 38 routes; platform toggle with wind-down; tenancy; permissions + NURSE; E2E bridge + 9 E2E tests; no-LLM guard; matrices IPD coverage; panel UI (4 pages); owner console card; Kriya Ketu (pages, 4 reads, 3 hand-offs, corpus +28, change-verb guard); docs set. First full run 20 failed / 6664 passed: 16 from a connection leak in the new test bridge (fixed: close all bridge connections at teardown), 3 Ketu gate updates, 1 OPD rollback-order test (fixed). Live UI journey verified with five real logins. |
| 2026-10-15 | Automated security review (MEDIUM): branch-pinned staff could change another branch's beds/wards (same clinic; cross-tenant was already blocked). Fixed: `_ward_in_branch()` guard on bed status, release hold, bed/ward edit and bed creation; worklist `beds_to_clean` scoped by branch. Regression `test_branch_pinned_staff_cannot_touch_another_branchs_beds`; `tests/test_ipd_api_e2e.py tests/test_ipd_no_llm.py tests/test_lint_unscoped_queries.py` → **16 passed**. |
| 2026-10-26 | Production audit: `POST /observations/{id}/correct` with an out-of-range or wrong-kind replacement raised an unhandled pydantic `ValidationError` (HTTP 500); now 422 and nothing is marked in error; the original kind is always kept. Regression in `tests/test_ipd_api_e2e.py`. Audit of migration 111 functions (locking order, CAS, accrual, final bill, discharge) found no other defect. IPD-17 (staging on Supabase) still NOT_STARTED. |
| 2026-10-26 | Multi-branch audit: (1) the panel's Add-ward form sent no branch, so every UI-made ward was branch-less and every branch-pinned user's bed board, census and cleaning list were empty; pinned staff now see their branch's plus shared (branch-less) wards, a pinned admin's new ward defaults to their branch, and the form has a branch picker for multi-branch plans. (2) assign-bed / reserve / transfer did not check the target bed's branch when the admission had none: a North user could place a patient in a South bed; `_ward_in_branch()` now guards the target. Tests: `tests/test_branch_isolation_real_pg.py`. IPD tariffs/settings are clinic-wide (owner decision). |
| 2026-10-26 | Ward branch can now be changed: `PATCH /admin/ipd/wards/{id}` accepts `branch_id` (null = shared). Branch must be this hospital's (404) and the pinned admin's own (403); only a hospital-wide admin can make a ward shared. Migration 114 trigger `ipd_guard_ward` refuses the move while any bed is occupied or reserved (`ipd_ward_in_use`, 409), locking the ward's beds first so a racing admission is either seen or reads the new branch. Panel: branch selector + Move on each ward card (multi-branch plans). Tests in `tests/test_branch_isolation_real_pg.py`, `tests/test_migration_114_draft_save_ward_branch.py`. |

## 7. Next exact action for the next agent
1. Read this file, then `git status` / `git log` to see what changed since.
2. Run the full suite with the venv (`.venv/Scripts/python.exe -m pytest -q`). If a real-Postgres test errors with
   "antivirus" or stale data, re-run that file alone (known `tmp_pg_pytest_data` quirk, OPD status harness notes).
   If 111 changed, re-apply it to `tmp_pg_pytest_data` first (the runner skips already-applied migrations).
3. Do **IPD-17** (staging) — it is the gate between "staging validation" and "pilot".
4. Then IPD-18, IPD-19, IPD-14 browser check. Do not start DEFERRED items without the owner decisions they list.

## 8. Release decision
**READY FOR CONTROLLED STAGING VALIDATION.** Not ready for a pilot: migration 111 has not run on Supabase/PostgREST
(IPD-17), the owner console card is not browser-verified (IPD-14), and owner decisions O-1…O-5 are not yet
accepted by a pilot hospital.
