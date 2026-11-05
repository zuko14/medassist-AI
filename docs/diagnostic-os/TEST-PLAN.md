# Kriya Diagnostic OS — Test and Verification Plan

> Part of the Diagnostic OS set: start at [status.md](status.md). Harness rules: `pytest` is hermetic
> (`tests/conftest.py` forces test credentials and fakes the distributed lock); **never** set `KRIYA_TEST_LIVE=1`;
> real-Postgres tests use the local embedded Postgres fixtures (`real_pg_conn`, `tmp_pg_pytest_data`) and the E2E
> bridge pattern of `tests/test_ipd_api_e2e.py`; after a killed run, re-run a real-PG file alone before calling it a
> regression (OPD status harness notes).

## 1. Standing gates — run at the end of EVERY phase; all must pass; paste results into status.md
```bash
# DG1 — tenancy linter (TOTAL_BARE_BASELINE stays 0; legacy annotations never rise)
python -m pytest tests/test_lint_unscoped_queries.py tests/test_phase4_scoped_queries.py -q
# DG2 — security matrices (must include every /admin/dx route; see §4)
python -m pytest tests/test_admin_super_admin_scope_matrix.py tests/test_phase2_route_adversarial_matrix.py tests/test_rls_security.py -q
# DG3 — zero-LLM: clinical firewall + IPD/OPD/DX AST guards
python -m pytest tests/test_clinical_firewall.py tests/test_opd_no_llm.py tests/test_ipd_no_llm.py tests/test_dx_no_llm.py -q
# DG4 — live channels unchanged (WhatsApp, voice, lab booking, home collection, lab report delivery)
python -m pytest tests/test_webhook.py tests/test_fsm_transitions.py tests/test_lab_test_booking_conversation.py \
  tests/test_lab_test_booking_payment.py tests/test_home_collection.py tests/test_lab_delivery_regressions.py \
  tests/test_lab_delivery_receipts.py tests/test_diagnostic_feature_gating.py -q
python -m pytest tests/voice/ -q
# DG5 — G1/G2/OPD/IPD money and LIS regression (must pass WITHOUT edits to these files)
python -m pytest tests/test_migration_115_orders.py tests/test_orders_api_e2e.py tests/test_migration_118_lis.py \
  tests/test_lis_api_e2e.py tests/test_opd_billing_real_pg.py tests/test_ipd_api_e2e.py -q
git diff --stat main -- tests/test_migration_115_orders.py tests/test_orders_api_e2e.py tests/test_migration_118_lis.py tests/test_lis_api_e2e.py   # expect empty
# DG6 — real-Postgres invariants + migration runner + all migration tests (rollback chain)
python -m pytest tests/test_real_postgres_invariants.py scripts/test_migration_runner.py "tests/test_migration_1*.py" tests/test_migration_dx_*.py -q
# DG7 — full suite, no -x; count >= baseline + new tests; 0 failed
python -m pytest -q -rs
```
If a gate fails: stop, fix, re-run the gate and DG7. Never skip, `xfail` or delete a test to get green; never raise a
linter baseline.

## 2. New test files by phase
| Phase | File | Must cover |
|:-|:-|:-|
| P1 | `tests/test_dx_feature_gate.py` | off by default; owner enable; partner account 400; enterprise wildcard alone does not enable; does not need OPD; `/admin/me.dx_access`; 409 preview → WIND_DOWN → re-enable loses nothing; OPD-on clinic unaffected; `_lis_scope` accepts DX clinic, still refuses a clinic with none of OPD/IPD/DX |
| P1 | `tests/test_migration_dx_m1.py` (real PG) | applies on top of 124; idempotent re-apply; verify block; rollback then re-apply |
| P1 | `tests/test_dx_no_llm.py` | AST: `app/services/dx*.py`, `app/routers/dx*.py` import nothing from AI/voice/assistant; AI/voice/assistant code never names `dx_` tables, `lab_results`, `lab_result_values` |
| P2 | `tests/test_dx_formula.py` (pure) | grammar whitelist (rejects `__import__`, attributes, subscripts, lambdas, unknown names, strings); all vectors of REPORT-TEMPLATES §1.1/§3; rounding modes; division by zero → not computed; cycle detection |
| P2 | `tests/test_dx_flags.py` (pure) | `high_exclusive/low_exclusive` defaults reproduce 118 flags exactly (vector table copied from `test_lis_api_e2e.py` expectations); exclusive bounds; delta check; auto-verify eligibility |
| P2 | `tests/test_migration_dx_m2.py` | component kinds/depth/cycle guard; approval cleared on clinical edit; unique test code; lab_tests rows created by the WhatsApp admin path still insert (defaults) |
| P2 | `tests/test_dx_catalogue_api.py` | seed apply idempotent, drafts inactive; approve with stale digest 409; import dry run |
| P3 | `tests/test_migration_dx_m3.py` | `opd_orders` shape: OPD/IPD rules unchanged, `dx` rules; doctor NULL only for dx; explode once (unique); bill line guard (wrong visit, cancelled item, price mismatch → refused); one live bill per visit; append-only items; purge |
| P3 | `tests/test_dx_visits_e2e.py` (HTTP + real PG) | W1, W2, W3 exactly; package explodes into per-test orders on the right tubes; idempotent register (10 parallel with one key → 1 visit); prepaid booking applied once; cancel before/after issue; G1 worklist excludes dx rows |
| P3 | `tests/test_dx_data_retention.py` | erasure redacts snapshots and keeps records |
| P3 | `tests/test_dx_existing_paths.py` | AUDIT A4/A5/A10/A20/A22 guards: G1 collect & complete refuse a dx order (also as a phlebotomist login); G1 worklist excludes dx; OPD Billing draft save on a DX invoice refused with a mapped error; lab-test delete refuses a test ordered in DX and still deletes an unused one; generic Lab Reports resend/resolve refuse `source='dx'`; `get_family_members` hides B2B dependants while OPD/WhatsApp family tests stay green unedited |
| P3 | `tests/test_dx_bookings.py` | 3 bookings (one test each, one a 20 % deposit) → one visit; each prepaid applied once (replay safe); home-collection fee item; balance = deposit shortfall; a booking cannot join two visits (parallel) |
| P3 | `tests/test_dx_b2b_subjects.py` | B2B subject without phone / with the desk phone → holder dependant; with own phone → registry patient; holder created once under concurrency |
| P4 | `tests/test_dx_samples.py` | grouping by container and processing site; barcode format and counter per `YYcc`; 20 parallel collections of the same sample → 1; pre-printed bind uniqueness (parallel); reject → replacement sample/orders; rejection after signing refused; `collect_before_payment` |
| P4 | `tests/test_dx_labels.py` (pure) | Code128 B/C encoding vs published vectors (check digit); label PDF bytes; ZPL contains `^BC` and the digits |
| P5 | `tests/test_dx_logistics.py` | routing precedence; batch add refuses wrong destination / second live batch; dispatch → in_transit; receive with missing/damaged/extra; stability timer computation (pure); processing-site user sees no phone |
| P6 | `tests/test_migration_dx_m6.py` | gate trigger: 3-level requires verification of current `updated_at`; re-entry invalidates; returned after verified invalidates; **gate ignores OPD/IPD orders**; verifications/critical events append-only; attachments only while unsigned |
| P6 | `tests/test_dx_results_e2e.py` | W8, W9; calculated values server-side (browser-sent values ignored); delta; four-eyes; batch with one stale item → that item refused, others signed; critical-call required blocks approval; LIS routes return `dx_use_dx_route` for dx orders; **signature on a dx order does not call `lis.deliver_report`** |
| P7 | `tests/test_dx_pdf.py` (pure) | Taiyo fixture renders: department and package headings, methods, multi-line reference text, bold flags, page `n of N`, QR, signature image, "Tested at" on white label, "Performed at" for outsourced; same input → same SHA-256; Indic names |
| P7 | `tests/test_dx_reports_e2e.py` | release when complete; partial; amended supersedes; shadow never calls `upload_and_send`; `dx` delivery owner refused with an active connector; resend idempotent; share link expiry/attempts; verify page fields only; **every row of DELIVERY-POLICY §7** |
| P7 | `tests/test_dx_delivery_policy.py` (real PG) | the decision table row by row (each refusal code reachable, order of rules); zero-total visit; draft invoice = unpaid; supplementary bill unpaid holds; void after payment re-holds new targets; field collection pending holds, handover releases; override expiry; payer kinds × `report_delivery` × authorisation; referrer consent; `plan_no_delivery` on `diagbooking`; per-target `external_report_id` (patient + referrer + account all delivered, AUDIT A3); `test_dx_single_sender` AST guard |
| P7 | `tests/test_dx_templates.py` | schema rejects unknown keys/HTML/foreign asset paths; publish immutability; cascade order |
| P8 | `tests/test_migration_dx_m8.py` | ledger append-only; one charge per item; statement totals = SQL sums; two parallel statements for one period → 1; overlapping barcode stock refused |
| P8 | `tests/test_dx_b2b_e2e.py` | W5 both variants, W14; hold by limit/overdue refuses new visits; prepaid insufficient refused; partner confinement (every non-partner route 403; another account's visit 404); white-label delivery to desk |
| P9 | `tests/test_dx_mis.py`, Ketu registry tests | MIS numbers reconcile with source rows; Ketu actions validate, no route drift, page table = sidebar |
| P10 | `tests/test_dx_bridge.py` | HMAC valid/invalid/stale/replayed; host query returns machine codes only for the tube's open orders; results idempotent; mapping; auto-enter off by default; Westgard rules (1-2s warning, 1-3s, 2-2s, R-4s, 4-1s, 10x) pure vectors |

## 3. Concurrency suite (real Postgres, threads with separate connections)
| Case | Expectation |
|:-|:-|
| 10 parallel `dx_register_visit` with one idempotency key | 1 visit, 9 replays |
| 20 parallel registrations | consecutive gapless `LAB-` numbers |
| 2 desks bind the same pre-printed barcode | 1 success, 1 `dx_barcode_in_use` |
| collect vs reject on one sample | one wins, the other `dx_sample_not_pending`; no half state |
| add the same sample to two batches | one live batch |
| verify vs re-entry race | either the verification matches the new version or approval is refused |
| two pathologists approve the same result | one signs, the other gets replay (`replayed`) or `lis_not_pending` |
| 10 parallel B2B registrations at the credit limit | total charges never exceed limit |
| 2 parallel statements for one account/period | 1 issued |
| release creation twice for the same results | one release; second returns existing |
| payment lands while a held delivery is being re-evaluated | exactly one send; never a send without payment |
| two workers claim the same queued delivery | one `sending`, the other gets `false` |
| visit `sample_seq` under 20 parallel registrations | unique per year; all ≥ `sample_seq_floor`; tubes of one visit share it |

## 4. Security matrices
`tests/test_admin_super_admin_scope_matrix.py` and `tests/test_phase2_route_adversarial_matrix.py` enumerate routes
from the app. Add assertions like the IPD one (`len([r for r in ROUTES if r[1].startswith("/admin/dx")]) >= N`) with
N updated per phase, so a new DX route cannot ship outside the matrix. Partner routes get their own matrix:
staff tokens vs partner tokens vs other-account partner tokens.

## 5. UI verification (per phase with UI)
Run the real app locally (`uvicorn app.main:app` with the embedded-PG bridge used for IPD) and drive the phase's
workflow with real logins for each role; check 1440 px and 390 px (no horizontal scroll), light and dark; confirm
request bodies match the pydantic models; record what was clicked and seen in status.md. Add a Playwright regression
for the desk → collect → enter → approve → release path in P11 (pre-installed Chromium; do not reinstall Playwright).

## 6. Staging verification (P11, before any pilot)
Apply 115–124 (if not yet), then DX-M1…Mn on a Supabase **branch**; run every `tests/test_dx_*e2e.py` with
`TEST_DATABASE_URL` pointing at it; run `migrations/verify_supabase_schema.sql`; check grants/RLS on every new table
(Session 51 method); record results.
