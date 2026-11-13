# Kriya Diagnostic OS — Execution Status (source of truth)

> **Every agent starts here.** Then read, in order: [AGENT-PROMPT.md](AGENT-PROMPT.md) (how to work) ·
> [ARCHITECTURE.md](ARCHITECTURE.md) · [ADR.md](ADR.md) · [DATA-MODEL.md](DATA-MODEL.md) · [WORKFLOWS.md](WORKFLOWS.md) ·
> [REPORT-TEMPLATES.md](REPORT-TEMPLATES.md) · [API.md](API.md) · [UI.md](UI.md) · [SECURITY.md](SECURITY.md) ·
> [TEST-PLAN.md](TEST-PLAN.md) · [ROLLOUT.md](ROLLOUT.md) · [KRIYA-KETU.md](KRIYA-KETU.md) ·
> [GAP-ANALYSIS.md](GAP-ANALYSIS.md) · [COMPETITIVE-ANALYSIS.md](COMPETITIVE-ANALYSIS.md) ·
> **[DELIVERY-POLICY.md](DELIVERY-POLICY.md)** (who may receive a report, when) · **[AUDIT.md](AUDIT.md)** (every design
> claim checked against code with file:line — read before touching any existing file).
>
> States: `NOT_STARTED` · `IN_PROGRESS` · `BLOCKED` (say why) · `IMPLEMENTED` (code written, evidence incomplete) ·
> `VERIFIED` (the listed command was run and passed — result pasted in §9) · `DEFERRED` (reason given).
> **Rules:** never mark VERIFIED without pasting the command's summary line; update this file in the same commit as
> the work; one task IN_PROGRESS per agent; never start a task whose dependencies are not VERIFIED.

## 0. Mission and scope
Build the **Kriya Diagnostic OS**: an optional, owner-enabled, tenant-isolated module that lets a diagnostic centre
(single lab, multi-branch chain, hub-and-spoke network, hospital lab with B2B clients) run its whole lab in Kriya —
registration → billing → tube labels → collection (centre, home, partner hospital) → transit → receipt → result entry
→ verification → pathologist approval → visit report on its own or a B2B client's letterhead → WhatsApp / portal /
print delivery → B2B statements — so no external LIMS and no browser-automation connector is needed. It reuses the
OPD registry and ledger, G1 orders (mig 115) and the G2 LIS (mig 118). **Existing behaviour (WhatsApp booking, voice,
home collection, MocDoc connector, CallMedex, OPD, IPD, lab-report delivery) must not change** for any clinic that has
not enabled DX, and for a DX clinic until a branch's delivery owner is switched to `dx`.

**Two owner rules that override every convenience (DELIVERY-POLICY §1):** (1) no report reaches the patient, the
referrer, a print for the patient or a share link while the visit's patient-payable amount is unpaid — only an audited
admin override; (2) insurance / TPA / corporate-panel visits are delivered to the payer's desk, never to the patient,
unless the account carries a recorded authorisation.

## 1. Current pointer (update at every pause — this is what the next agent resumes from)
| Field | Value |
|:-|:-|
| Active phase | **P2 — Catalogue** (P0 and P1 VERIFIED 2026-11-13) |
| Next task | **DX-12** (migration DX-M2, next free number 126) |
| Working branch | `dx/p1-foundation` (from `main` @ `94de8ae`; not pushed, no PR) |
| Last commit touching DX | `feat(dx): P0 baseline + P1 foundation (DX-00…DX-11)` on `dx/p1-foundation` |
| Last VERIFIED gate | P1 DG1–DG7 (§9, 2026-11-13) |
| Open blockers | none for code. Migration 125 is **not applied** to Supabase (owner action, ROLLOUT §0.4: apply before deploying this code) |
| Lanes running in parallel | none |

## 2. Resume protocol (after a pause of any length)
1. `git fetch origin && git status && git log --oneline -15` — see what landed since §1 was written.
2. Read §1, §4 and §6 of this file and the §10 log tail. Read AGENT-PROMPT.md §1.
3. Re-run the **last VERIFIED gate** of the active phase (§4 lists it). If red: the cause is new code on `main`; fix
   or report before continuing — never build on a red baseline.
4. Check every migration file added to `main` since §5's last row; if a newer migration took "your" number, renumber
   your pending file (ADR-DX-18).
5. Continue with §1 "Next task". If that task is IN_PROGRESS by an agent that stopped, inspect its branch, finish or
   restart it, and note the takeover in §10.

## 3. Discovery and baseline (P0) — fill before any code
| Item | Result |
|:-|:-|
| Repo / branch / HEAD | P0 started at `main` @ `94de8ae` (2026-11-13, "Merge pull request #8"); untracked `docs/PROJECT_DOCUMENTATION.md`, `.DS_Store` left alone |
| Latest migration on disk | `124_insurance_tpa_and_cashless.sql` at start → DX-M1 took **125** (`125_dx_os_foundation.sql`) |
| Migrations 115–124 applied to Supabase? | **Not verified** this session (no Supabase access). Still assumed not applied; DX staging needs 115, 118 and 125 applied first |
| Python / runner | macOS, `.venv/bin/python` = Python 3.12.15, pytest 8.0.0; real-Postgres tests use `pgserver` (`tmp_pg_pytest_data`) |
| **Baseline full suite** | `python -m pytest -q -rs` at `94de8ae` (clean worktree) → **8443 passed, 5 skipped, 0 failed** in 293.84 s. Skips: 4 × `KRIYA_TEST_LIVE` production reads, 1 × MocDoc live sandbox |
| Pre-existing failures | **none** |
| Facts F1–F16 (GAP-ANALYSIS §3) and AUDIT A1–A29 evidence still true at HEAD | **Confirmed, no deltas.** `opd_enabled` still requires `booking` (F1; `diagstream` has none); `_lis_scope` `app/routers/lis.py:100` and orders `_dx_scope` `app/routers/orders.py:91` required `opd_enabled or ipd_access` (F2); `opd_orders.source IN ('opd','ipd')`, `doctor_id NOT NULL`, `opd_orders_source_shape` at `115_…:41/48/79` (F3); `spawn_background_task(deliver_report` at `app/services/lis.py:389` (F4); `collect_sample` / `complete_order` `app/services/orders.py:262/282` have no `source` check (F12/A4); `upload_and_send` `lab_reports.py:230`, `template_name_for` `:24`, `_PHLEBOTOMIST_ROUTES` `admin.py:476`, `get_family_members` `database.py:1745`, `_apply_prepaid` `opd_billing.py:447`, `_validate_and_price_items` `opd_billing.py:1545`, `get_review_queue` `lis.py:438`, `route_recipient_for_provider` `report_routing.py:83`, `report_dispatch_allowed` `subscription.py:355`, `opd_next_counter` `103_…:195`, `patients_clinic_phone_key` `035_…:112`, `idx_lab_reports_clinic_external_report` mig 026. Other `opd_orders` readers (for DX-19): `clinical_documents.py`, `masters.py`, `opd_billing.py`, `radiology.py` (3) — as AUDIT §2 |
| Owner decisions O-DX-1…11 | not re-asked; **defaults in force** (ADR.md "Open owner decisions") |

## 4. Phase board
| Phase | Scope | Tasks | Depends on | Can run in parallel with | Migration | Gate to close | State |
|:-|:-|:-|:-|:-|:-|:-|:-|
| P0 | Discovery & baseline | DX-00–01 | — | — | — | §3 filled | VERIFIED |
| P1 | Foundation: entitlement, permissions, scope, sites, owner toggle, setup page | DX-02–11 | P0 | Lane PURE (DX-13, DX-27a, DX-43a) | DX-M1 | DG1–DG7 | VERIFIED |
| P2 | Catalogue: departments, containers, signatories, test master, parameters, formulas, seed, approval | DX-12–17 | P1 | Lane PURE | DX-M2 | DG1–DG7 | NOT_STARTED |
| P3 | Registration, visits, orders (`source=dx`), billing, booking conversion | DX-18–25 | P2 | — | DX-M3 | DG1–DG7 | NOT_STARTED |
| P4 | Samples, labels, collection (desk + home), rejection, same-site receipt | DX-26–30 | P3 | P8a | DX-M4 | DG1–DG7 | NOT_STARTED |
| P5 | Logistics: routing, batches, hub receipt, outsourcing | DX-31–35 | P4 | P6, P8a | DX-M5 | DG1–DG7 | NOT_STARTED |
| P6 | Bench: stage worklists, entry with formulas/delta, verification, approval (single/batch), critical calls | DX-36–40 | P4 | P5, P8a | DX-M6 | DG1–DG7 | NOT_STARTED |
| P6b | OPD/IPD lab orders through the DX bench | DX-41 | P6 | P7 | (in DX-M6 or own) | DG1–DG7 | NOT_STARTED |
| P7 | Reports: renderer, templates, releases, dues hold, delivery, shadow mode, public verify/share | DX-42–47 | P6 (P5 for outsourced line) | P8a | DX-M7 | DG1–DG7 | NOT_STARTED |
| P8a | B2B money: rate cards, ledger, holds, statements, payments | DX-48–50 | P3 | P4–P7 | DX-M8 | DG1–DG7 | NOT_STARTED |
| P8b | Partner portal, white-label delivery, barcode stock, B2B & referrer pages | DX-51–53 | P7, P8a | P9 | (DX-M8) | DG1–DG7 | NOT_STARTED |
| P9 | Dashboard, MIS, Ketu, voice, WhatsApp intents | DX-54–58 | P7 | P8b, P10 | — | DG1–DG7 | NOT_STARTED |
| P10 | Analyser bridge, instrument results, QC | DX-59–62 | P6 | P7–P9 | DX-M9 | DG1–DG7 | NOT_STARTED |
| P11 | Hardening, staging, docs, sandbox run, pilot readiness | DX-63–66 | all above that ship | — | — | TEST-PLAN §6 + ROLLOUT §1 | NOT_STARTED |

Lane PURE = modules with no DB or route dependency (formula engine, Code128/labels, PDF renderer + template schema)
that may be built from P1 onward by a separate agent (AGENT-PROMPT.md §3).

## 5. Migration register (numbers assigned at merge time — ADR-DX-18)
| DX id | File | Merged in commit | Applied to staging | Applied to production |
|:-|:-|:-|:-|:-|
| DX-M1 | `125_dx_os_foundation.sql` (+ `rollback/125_rollback_dx_os_foundation.sql`) | `dx/p1-foundation` (not merged) | no | no |
| DX-M2 | — | — | — | — |
| DX-M3 | — | — | — | — |
| DX-M4 | — | — | — | — |
| DX-M5 | — | — | — | — |
| DX-M6 | — | — | — | — |
| DX-M7 | — | — | — | — |
| DX-M8 | — | — | — | — |
| DX-M9 | — | — | — | — |

## 6. Tasks
Acceptance = what must be true; Tests = files that prove it (TEST-PLAN §2). Every task also requires: tenancy linter
green, audit on writes, mapped errors, docs updated in the same commit.

### P0 — Discovery & baseline
| ID | Task | Depends | Acceptance | Tests | State |
|:-|:-|:-|:-|:-|:-|
| DX-00 | Read AGENTS.md, this folder, `docs/hospital-os/status.md`, `docs/sessions/SESSION_44_G2_LIS.md`, `SESSION_41`, `SESSION_28`; record §3 (HEAD, latest migration, Supabase state of 115–124, runner, **baseline full suite**, pre-existing failures, owner decisions) | — | §3 complete | full suite | VERIFIED |
| DX-01 | Re-verify F1–F16 at HEAD: `grep -n "def opd_enabled" -A15 app/services/tenant.py`; `grep -n "_lis_scope" -A12 app/routers/lis.py`; `grep -n "opd_orders_source_shape\|doctor_id" migrations/115_*.sql`; `grep -n "spawn_background_task(deliver_report" app/services/lis.py`; `grep -rn "opd_orders" app --include=*.py \| grep -v "^app/services/orders.py\|^app/services/lis.py"` (list every reader for DX-19); **and every AUDIT.md §1/§2 evidence line** (re-locate by symbol if lines moved: `upload_and_send`, `idx_lab_reports_clinic_external_report`, `collect_sample`, `complete_order`, `_PHLEBOTOMIST_ROUTES`, `get_family_members`, `_apply_prepaid`, `_validate_and_price_items`, `resend_report`, `opd_next_counter`, `patients_clinic_phone_key`); record deltas in §7 | DX-00 | F1–F16 and AUDIT A1–A29 confirmed or design updated | — | VERIFIED |

### P1 — Foundation (DX-M1)
| ID | Task | Depends | Acceptance | Tests | State |
|:-|:-|:-|:-|:-|:-|
| DX-02 | Migration DX-M1 + rollback + verify block (`clinics.dx_state`, `dx_settings`, `dx_sites`, `dx_purging`, `dx_purge_clinic` v1); add to `verify_supabase_schema.sql` | DX-01 | applies on latest; idempotent; rollback + re-apply | `test_migration_dx_m1.py` | VERIFIED |
| DX-03 | `app/services/tenant.py`: `OPT_IN_FEATURES["dx_enabled"]`, `dx_eligible`, `dx_access` (ADR-DX-02) | DX-02 | off by default; wildcard alone never enables; partner refused; no OPD needed | `test_dx_feature_gate.py` | VERIFIED |
| DX-04 | `app/services/permissions.py`: DX permissions, new roles, presets, role-implied grants (SECURITY §3), `holds_dx_permission`; staff grant cap respected | — | existing logins gain nothing except listed implied grants | `test_dx_feature_gate.py`, existing permission tests | VERIFIED |
| DX-05 | `app/routers/dx.py` + `app/services/dx.py`: `_dx_scope`, site-scope helper `_visit_for` skeleton, `provision_defaults` (sites from branches, departments, containers, rejection reasons later), `deactivation_preview`, routes `/setup`, `/settings` (CAS), `/sites`, `/go-live`; include router in `app/main.py` | DX-03, DX-04 | every route checks permission → tenant → entitlement → wind-down | `test_dx_feature_gate.py` | VERIFIED |
| DX-06 | `app/routers/platform.py`: `dx_enabled` branch (eligibility 400, provision, 409 preview, confirm → WIND_DOWN, re-enable), `GET …/dx/deactivation-preview`, purge order (`dx_purge_clinic` first); `admin/platform.html` DX card `toggleDx` | DX-05 | ADR-DX-14 table holds | `test_dx_feature_gate.py` | VERIFIED |
| DX-07 | `app/routers/admin.py`: `/admin/me` → `dx_access`, `dx_state`, `dx_site_ids`; `verify_credentials` confinement for `LOGISTICS_RUNNER` nav | DX-04 | | `test_dx_feature_gate.py` | VERIFIED |
| DX-08 | Widen `_lis_scope` (`app/routers/lis.py`) and the orders scope (`app/routers/orders.py`) with `or dx_access(clinic)` | DX-03 | OPD/IPD clinics unchanged; DX clinic reaches LIS; clinic with none refused | `test_dx_feature_gate.py`; DG5 unedited | VERIFIED |
| DX-09 | `app/tenancy.py` registration of DX-M1 tables; matrices assert `/admin/dx` route count | DX-05 | DG1, DG2 green | DG1, DG2 | VERIFIED |
| DX-10 | `tests/test_dx_no_llm.py` (AST guard, ADR-DX-10) | DX-05 | | itself | VERIFIED |
| DX-11 | `admin/index.html`: `data-dx` gating, `applyDxGates()`, sidebar group, **DX Setup** page (checklist, sites, settings) | DX-05–07 | live UI check 1440/390 px | manual (record §9) | VERIFIED |

**P1 Definition of Done:** no existing clinic's behaviour changed (no clinic has `dx_enabled`); owner can enable /
wind-down / re-enable DX on a sandbox clinic; DX Setup reachable only when enabled; DG1–DG7 green, counts pasted.

### P2 — Catalogue (DX-M2)
| ID | Task | Depends | Acceptance | Tests | State |
|:-|:-|:-|:-|:-|:-|
| DX-12 | Migration DX-M2 (departments, containers, signatories, `lab_tests` + `lab_test_parameters` additive columns incl. `low/high_exclusive`, components + guard, approval-clearing trigger) | P1 | existing lab-test admin and WhatsApp catalogue tests pass unedited | `test_migration_dx_m2.py`, `test_lab_tests_*.py` | NOT_STARTED |
| DX-13 | `app/services/dx_formula.py` (Lane PURE) | — | all REPORT-TEMPLATES §1.1/§3 vectors | `test_dx_formula.py` | NOT_STARTED |
| DX-14 | `app/services/lis.py`: `flag_value` honours `low/high_exclusive` with False defaults; pure `delta_check`, `auto_verify_eligible` in `dx_results.py` | DX-12 | 118 flag vectors unchanged | `test_dx_flags.py`, DG5 | NOT_STARTED |
| DX-15 | Catalogue routes (API P2): departments, containers, signatories + signature upload, catalog list/edit, components, approve (digest CAS), formula check; LIS parameter routes accept DX fields and validate formulas | DX-12, DX-13 | | `test_dx_catalogue_api.py` | NOT_STARTED |
| DX-16 | Seed library `app/assets/dx_seed/tests_v1.json` (~150 tests, indicative bands marked) + seed preview/apply + CSV/XLSX parameter import with dry run | DX-15 | drafts inactive & unapproved; idempotent | `test_dx_catalogue_api.py` | NOT_STARTED |
| DX-17 | UI **Test Master** page | DX-15 | live UI | manual | NOT_STARTED |

**P2 DoD:** a lab director can seed/import, edit bands/formulas, preview calculations, and approve tests; WhatsApp
booking of the same tests unchanged.

### P3 — Registration, visits, billing (DX-M3)
| ID | Task | Depends | Acceptance | Tests | State |
|:-|:-|:-|:-|:-|:-|
| DX-18 | Migration DX-M3 (referrers, b2b accounts identity, accessions, items, samples table, events, `opd_orders` extension, invoice links, line guard, functions `dx_register_visit` / `dx_add_items` / `dx_cancel_item` / `dx_cancel_visit` / `dx_bill_visit`, purge v2) | P2 | ADR-DX-03 constraints; 115 suite unedited | `test_migration_dx_m3.py`, DG5 | NOT_STARTED |
| DX-19 | Guard every existing path against DX rows (AUDIT A4, A10, A20, A22): G1 worklist `source IN ('opd','ipd')`; G1 `collect_sample` / `complete_order` refuse `source='dx'` (409 `dx_use_dx_route`); DX bill-line guard trigger (OPD draft save on a DX bill refused, mapped message in `opd_billing`); generic Lab Reports resend / resolve refuse `source='dx'` rows; lab-test delete refuses a test ordered in DX | DX-18 | OPD/IPD behaviour identical | `test_dx_existing_paths.py`, DG5 | NOT_STARTED |
| DX-20 | `app/services/dx.py`: price quote, register (pricing in SQL), add/cancel items, visit read model with field filtering, timeline | DX-18 | W1/W2 steps 1–3 | `test_dx_visits_e2e.py` | NOT_STARTED |
| DX-21 | Routes: patient search/register (registry service under `_dx_scope`), referrers, B2B identity CRUD incl. payer kinds and the payer-routed CHECK, visits CRUD (API P3); B2B subjects without own phone → holder-patient dependants with `family_members.dx_b2b_account_id`; `get_family_members` hides them (ADR-DX-21) | DX-20 | conversation family tests unedited | `test_dx_visits_e2e.py`, `test_dx_b2b_subjects.py`, `test_dx_existing_paths.py` | NOT_STARTED |
| DX-22 | Billing glue: bill/issue/receipts/refunds/payment link via `opd_billing`; invoice `appointment_id` NULL; `_apply_prepaid` once per linked booking; `fee` items; supplementary bill for items added after issue; bill PDF A5 + 80 mm | DX-20 | Σ live invoices = Σ patient shares; each prepaid once | `test_dx_visits_e2e.py`, `test_dx_bookings.py` | NOT_STARTED |
| DX-23 | Today's bookings list + convert (all of a patient's bookings for the day → one visit via `dx_accession_bookings`; prepaid applied per booking) | DX-22 | W3 | `test_dx_visits_e2e.py` | NOT_STARTED |
| DX-24 | `data_retention.py` DX section | DX-18 | | `test_dx_data_retention.py` | NOT_STARTED |
| DX-25 | UI **Registration** page (incl. Today's bookings) | DX-21–23 | live UI, keyboard flow | manual | NOT_STARTED |

**P3 DoD:** W1 steps 1–3, W2, W3, W12 run end-to-end on the sandbox; concurrency cases 1–2 (TEST-PLAN §3) pass.

### P4 — Samples and collection (DX-M4)
| ID | Task | Depends | Acceptance | Tests | State |
|:-|:-|:-|:-|:-|:-|
| DX-26 | Migration DX-M4 (rejection reasons, sample guard, collect / bind / reject / receive-same-site / store functions) | P3 | ADR-DX-05 | `test_dx_samples.py` | NOT_STARTED |
| DX-27 | `app/services/dx_barcode.py` Code128 B/C (Lane PURE = DX-27a) + label PDF + ZPL in `dx_pdf.py` | — | published vectors | `test_dx_labels.py` | NOT_STARTED |
| DX-28 | Routes: collection queue, label print dialog (per sample / per test / register-book label, reprint log), bind, collect, reject (+ recollection WhatsApp: in-session or `dx_recollection_required` template), receive same-site, track sample | DX-26, DX-27 | W1 steps 4–6, W7; barcode vectors of REPORT-TEMPLATES §6 | `test_dx_samples.py`, `test_dx_labels.py` | NOT_STARTED |
| DX-29 | Home collection: `prepare-route`, phlebotomist samples + field-collection routes added to `_PHLEBOTOMIST_ROUTES`, portal Samples / Cash collected panel, cashier handover (`dx_field_collections`) | DX-28 | W4; phlebotomist still confined; handover creates exactly one receipt | `test_dx_samples.py`, `test_home_collection.py` unedited | NOT_STARTED |
| DX-30 | UI **Sample Collection** page | DX-28 | live UI, scanner + camera | manual | NOT_STARTED |

### P5 — Logistics and outsourcing (DX-M5)
| ID | Task | Depends | Acceptance | Tests | State |
|:-|:-|:-|:-|:-|:-|
| DX-31 | Migration DX-M5 | P4 | | `test_dx_logistics.py` | NOT_STARTED |
| DX-32 | Routing rules CRUD + resolution inside `dx_register_visit` (`CREATE OR REPLACE`) + preview route | DX-31 | precedence; tubes split by destination | `test_dx_logistics.py` | NOT_STARTED |
| DX-33 | Batches (create/add/dispatch/receive), stability timers (pure), `LOGISTICS_RUNNER` nav, `dx_sample_stability_scan` job | DX-31 | W6 | `test_dx_logistics.py` | NOT_STARTED |
| DX-34 | Outsource partners + orders `outsource_partner_id` + "performed at" data for P7 | DX-32 | W13 (data part) | `test_dx_logistics.py` | NOT_STARTED |
| DX-35 | UI **Logistics** page | DX-33 | live UI | manual | NOT_STARTED |

### P6 — Bench, verification, approval (DX-M6)
| ID | Task | Depends | Acceptance | Tests | State |
|:-|:-|:-|:-|:-|:-|
| DX-36 | Migration DX-M6 (meta, verifications, critical events, attachments, **gate trigger**, `dx_record_results`, `dx_verify_result`, `dx_approve_results`) | P4 | gate ignores OPD/IPD | `test_migration_dx_m6.py` | NOT_STARTED |
| DX-37 | LIS: skip `deliver_report` for `source='dx'`; LIS entry/validate routes return `409 dx_use_dx_route` for dx orders; `get_review_queue` excludes DX orders (AUDIT A5) | DX-36 | F4 closed; OPD/IPD unchanged | `test_dx_results_e2e.py`, DG5 | NOT_STARTED |
| DX-38 | `app/services/dx_results.py`: entry (formulas, delta, meta, critical detection), verify/return, approve single + batch, critical-call records, attachments | DX-36, DX-13, DX-14 | W8, W9 | `test_dx_results_e2e.py` | NOT_STARTED |
| DX-39 | Stage read model `dx_stage()` + `/worklist` + TAT (two clocks) + `dx_tat_breach_scan` job | DX-38 | ARCHITECTURE §4.2 | `test_dx_results_e2e.py` | NOT_STARTED |
| DX-40 | UI **Lab Worklist**, **Worksheet** drawer, **Approval** page | DX-38, DX-39 | live UI | manual | NOT_STARTED |
| DX-41 | P6b: `dx_adopt_orders` — OPD/IPD lab orders as internal visits on the DX bench (W15) | DX-40 | OPD/IPD bills untouched | new E2E + DG5 | NOT_STARTED |

### P7 — Reports, templates, delivery (DX-M7)
| ID | Task | Depends | Acceptance | Tests | State |
|:-|:-|:-|:-|:-|:-|
| DX-42 | Migration DX-M7 | P6 | release/template guards | `test_dx_reports_e2e.py` | NOT_STARTED |
| DX-43 | `dx_pdf.py` visit report renderer (Lane PURE = DX-43a with fixture dicts) + template schema (pydantic) | — | REPORT-TEMPLATES §5; deterministic SHA | `test_dx_pdf.py`, `test_dx_templates.py` | NOT_STARTED |
| DX-44 | Templates CRUD/assets/publish/retire/preview/clone-from-PDF + 3 starter templates + cascade | DX-42, DX-43 | ADR-DX-08 | `test_dx_templates.py` | NOT_STARTED |
| DX-45 | Releases and deliveries per [DELIVERY-POLICY.md](DELIVERY-POLICY.md): `dx_delivery_decision`, per-target `dx_release_deliveries` with ids `dx-<release>-p\|r\|a`, `dx_claim_delivery` CAS before the single sender, `dx_outbox` triggers + `dx_outbox_dispatch` job, overrides, print log, payment-due link, account targets `ai_summary=False`, delivery owner + shadow + connector guard; render + upload in background (`spawn_background_task`) | DX-44 | W5-C, W10, W11, ADR-DX-15/19/20; every row of DELIVERY-POLICY §7 | `test_dx_reports_e2e.py`, `test_dx_delivery_policy.py` | NOT_STARTED |
| DX-46 | Public verify + share links (rate limits, attempts, expiry) | DX-45 | SECURITY §6 | `test_dx_reports_e2e.py` | NOT_STARTED |
| DX-47 | UI **Reports & Dispatch** + **Report Templates** pages | DX-44, DX-45 | live UI | manual | NOT_STARTED |

### P8a / P8b — B2B (DX-M8)
| ID | Task | Depends | Acceptance | Tests | State |
|:-|:-|:-|:-|:-|:-|
| DX-48 | Migration DX-M8 | P3 | ledger invariants | `test_migration_dx_m8.py` | NOT_STARTED |
| DX-49 | Rate cards + resolution in `dx_register_visit`; `dx_b2b_post_charge` / reversal wired into register/add/cancel (`CREATE OR REPLACE`) | DX-48 | B2B visit creates charges; patient visit unaffected | `test_dx_b2b_e2e.py` | NOT_STARTED |
| DX-50 | Payments, holds (manual + `dx_b2b_hold_evaluation` job), statements + PDF + WhatsApp, receivables ageing | DX-49 | W14; concurrency cases | `test_dx_b2b_e2e.py` | NOT_STARTED |
| DX-51 | `B2B_PARTNER` role, `dx_b2b_account_id`, confinement in `verify_credentials`, partner routes, partner portal UI | P7, DX-50 | W5-A; partner matrix | `test_dx_b2b_e2e.py` | NOT_STARTED |
| DX-52 | Barcode stock + bind check | DX-48, DX-28 | non-overlap | `test_migration_dx_m8.py` | NOT_STARTED |
| DX-53 | UI **B2B Accounts** + **Referrers** pages; white-label delivery to account desk (generalises `report_routing`) | DX-50, DX-45 | W5 | `test_dx_b2b_e2e.py`, manual | NOT_STARTED |

### P9 — MIS and assistants
| ID | Task | Depends | Acceptance | Tests | State |
|:-|:-|:-|:-|:-|:-|
| DX-54 | `/dashboard`, `/mis/*`, CSV export | P7 | numbers reconcile | `test_dx_mis.py` | NOT_STARTED |
| DX-55 | Ketu registry entries (KRIYA-KETU §1) | DX-54 | validate + drift + RT-14 | `test_assistant_registry.py` | NOT_STARTED |
| DX-56 | Voice `report_status` / `resend_report` via `dx_status.py` (status only) | DX-45 | no values reachable from voice | `tests/voice/…`, `test_dx_no_llm.py` | NOT_STARTED |
| DX-57 | WhatsApp report-status intent + resend button | DX-45 | FSM tests unchanged | `test_fsm_transitions.py` + new | NOT_STARTED |
| DX-58 | UI **DX Dashboard** | DX-54 | live UI | manual | NOT_STARTED |

### P10 — Instruments and QC (DX-M9)
| ID | Task | Depends | Acceptance | Tests | State |
|:-|:-|:-|:-|:-|:-|
| DX-59 | Migration DX-M9 | P6 | | `test_dx_bridge.py` | NOT_STARTED |
| DX-60 | Bridge API (HMAC, replay, idempotency), agents/instruments admin, mapping, worksheet prefill, optional auto-enter | DX-59 | ADR-DX-13 | `test_dx_bridge.py` | NOT_STARTED |
| DX-61 | Reference bridge (`connectors/bridge/`): ASTM E1381/E1394 + HL7 v2 ORU parsers, serial/TCP, outbound queue with replay | DX-60 | parser vectors from vendor manuals | own tests | NOT_STARTED (may be DEFERRED by owner) |
| DX-62 | QC materials/results, Westgard rules, LJ chart UI | DX-59 | rule vectors | `test_dx_bridge.py` | NOT_STARTED |

### P11 — Hardening and release
| ID | Task | Depends | Acceptance | Tests | State |
|:-|:-|:-|:-|:-|:-|
| DX-63 | Full matrices, partner matrix, no-LLM, full regression, Playwright regression of desk → release | all shipping phases | DG1–DG7 | all | NOT_STARTED |
| DX-64 | Staging: apply 115–124 (if needed) + DX migrations on a Supabase branch; E2E with `TEST_DATABASE_URL`; grants/RLS check | DX-63 | TEST-PLAN §6 | E2E | NOT_STARTED |
| DX-65 | Docs: `docs/agent-context/03,04,05,08,09,13` DX sections; AGENTS.md table; `docs/hospital-os/status.md` G2 board drift (F8) | DX-63 | | — | NOT_STARTED |
| DX-66 | Sandbox clinic run of W1–W14 in production; ROLLOUT §3 prerequisites recorded | DX-64 | | manual | NOT_STARTED |

### Deferred (do not start without an owner decision)
| ID | Item | Why deferred |
|:-|:-|:-|
| DX-DEF-ABDM | ABDM M2: DiagnosticReportLab FHIR bundle to PHR | with Hospital OS G10 (M1+M2 together) |
| DX-DEF-INV | Reagent / consumable inventory | needs the G3 stock-ledger pattern |
| DX-DEF-OFFLINE | Offline collection (PWA queue) | conflict-resolution design; network-dependent today |
| DX-DEF-REFLEX | Reflex / add-on test rules | clinical governance |
| DX-DEF-X | Sample exchange between two Kriya tenants (hospital tenant ↔ lab tenant) | cross-tenant contract design |
| DX-DEF-PRINTAGENT | Direct USB label printing via a local print agent (e.g. QZ Tray) | PDF/ZPL covers launch |
| DX-DEF-EMAIL-SMS | E-mail and SMS delivery of reports | no e-mail/SMS provider in the stack today |

## 7. Deviation log (design changes made during implementation)
| Date | Task | Change | Why | Docs updated |
|:-|:-|:-|:-|:-|
| 2026-11-13 | DX-02 | Added `dx_update_settings(uuid, jsonb, integer)`: the settings PATCH is a compare-and-swap on `dx_settings.version` in one SQL statement (409 `dx_settings_conflict`) | DATA-MODEL §2 asked for "CAS on PATCH" without a mechanism; a read-then-write in Python would lose updates | DATA-MODEL §2 |
| 2026-11-13 | DX-02 | `dx_purging` / `dx_purge_clinic` are `SECURITY DEFINER` with `search_path` pinned; rollback file named `125_rollback_dx_os_foundation.sql` | owner brief for this phase (house convention elsewhere: plain functions, `NNN_down.sql`); EXECUTE revoked from PUBLIC/anon/authenticated | — |
| 2026-11-13 | DX-04 | `role_grants_beyond_authority` ignores the DX grants implied by the pre-DX lab roles (`DIAGNOSTIC_OPERATOR`, `LAB_OPERATOR`, `PATHOLOGIST`); the new DX roles stay capped | otherwise a staff granter without `DX_READ` would newly be refused creating a lab technician on a clinic that never enabled DX (Session 51 cap) | — |
| 2026-11-13 | DX-04 | Clinic-conditional presets for existing roles (FRONT_DESK/CASHIER "+DX_* when created on a DX clinic", SECURITY §3.2) **not implemented**; the staff page offers the 14 DX permission checkboxes (`data-dx="perm"`, shown only on DX clinics) but not yet the 5 new roles | `ROLE_PRESETS` is static and has no clinic; DX grants on those roles have no route to use before P3. Clinic admins can grant via the API. Do with DX-25 (desk UI) | — |
| 2026-11-13 | DX-06 | Owner console reads DX state/eligibility from `GET /platform/clinics/{id}/dx/deactivation-preview` (select `*`); clinic detail select not extended with `dx_state`; purge calls `dx_purge_clinic` only when the clinic row has `dx_state` | an explicit `dx_state` column in shared queries would 500 the owner console / clinic deletion if code deploys before 125 is applied | — |
| 2026-11-13 | DX-05 | `deactivation_preview` returns zeros (`blocking=false`) | no visits/samples/results exist before DX-M3/M4/M6; each of those must add its counts (marked `shortcut:` in `app/services/dx.py`) | — |
| 2026-11-13 | DX-11 | A "Diagnostics" sidebar group already existed (Session 52, LIS / worklist / radiology for OPD+IPD). DX Setup is a `data-dx` link inside it, not a new group gated by `dx_access` | gating the existing group by `dx_access` would hide the lab pages from every OPD/IPD clinic; group visibility already follows its visible links | UI.md §1 unchanged in intent |

## 8. Known issues and limitations
- At authoring, migrations 115–124 (which DX builds on) are not applied to Supabase; DX cannot reach staging first.
- `admin/index.html` is a very large single file — UI lanes must use the DX markers (UI.md §1) to avoid conflicts.
- Re-audit 2026-11-05 ([AUDIT.md](AUDIT.md)) corrected the first draft in 29 places (2 critical: dues gating and payer
  routing were incomplete). Agents must treat AUDIT.md as binding where it differs from memory of the earlier draft.
- Approval requires doctor-linked logins with registration numbers; centres where non-doctor scientists sign
  reports under state rules need an owner decision (signatory eligibility is data in `dx_signatories`, but the 118
  CHECK requires a registration number).

## 9. Verification evidence (paste command and summary line; newest first)
| Date (IST) | Task / gate | Command | Result |
|:-|:-|:-|:-|
| 2026-11-13 06:55 | P1 / DG7 | python -m pytest -q -rs | 8502 passed, 5 skipped, 15 warnings in 295.27s (baseline 8443 + 59 new; same 5 skips) |
| 2026-11-13 06:35 | P1 / DG5 | python -m pytest tests/test_orders_api_e2e.py tests/test_lis_api_e2e.py -q | 17 passed, 2 warnings in 1.93s |
| 2026-11-13 06:35 | DX-02…09 | python -m pytest tests/test_dx_feature_gate.py tests/test_migration_dx_m1.py -q | 32 passed, 2 warnings in 1.34s |
| 2026-11-13 06:35 | DX-10 / DG3 | python -m pytest tests/test_clinical_firewall.py tests/test_dx_no_llm.py -q | 36 passed, 1 warning in 0.18s |
| 2026-11-13 06:35 | DX-09 / DG2 | python -m pytest tests/test_admin_super_admin_scope_matrix.py tests/test_phase2_route_adversarial_matrix.py -q | 1140 passed, 2 warnings in 3.12s |
| 2026-11-13 06:35 | DX-09 / DG1 | python -m pytest tests/test_lint_unscoped_queries.py tests/test_phase4_scoped_queries.py -q | 10 passed, 1 warning in 20.37s |
| 2026-11-13 06:30 | DX-11 live UI | real app + real Postgres (pgserver + `tests/ipd_pg_bridge.py`), seeded `diagstream` lab, 3 branches | **clinic_admin @1440 px**: Diagnostics group shows "DX Setup"; checklist (Sites ✓, Departments/Containers/Signatories "Arrives with the Test Master", Settings); added site KPHB (processing, NABL MC-5521) → toast, table updated, add form hides when every branch has a site; settings approval 3 + prefix `bh-` → saved as `BH-`, version 1; stale-version save → 409 toast "Someone else changed these settings…", form reloads server values; Go live → "Live since 13 Nov 2026", button hidden, version 2. **@390 px**: page width 390 (no horizontal scroll), sites table collapses to labelled cards. **LAB_OPERATOR**: nav visible (implied DX_READ), settings fieldset disabled, no edit buttons. **FRONT_DESK**: DX nav hidden. **PROCESSING_ADMIN pinned to Kukatpally**: sees only KPHB, can edit it, settings read-only. **Owner console** (`/platform-panel`): Diagnostic OS card "State: ACTIVE"; toggle off → "State: OFF"; on → "State: ACTIVE". Console: no DX errors (only the deliberate 409 and existing dashboard 403s for staff logins) |
| 2026-11-13 06:05 | DX-02 rollback chain | python -m pytest tests/test_migration_124_insurance.py tests/test_migration_103_opd.py tests/test_migration_111_ipd.py -q -k "rollback or down or reapply" | 4 passed, 57 deselected in 5.63s (`unwind_124` now unwinds 125 first) |
| 2026-11-13 06:05 | DX-04 | python -m pytest tests/test_permissions.py tests/test_staff_branch_delegation_and_profile.py tests/test_specialty_plans.py tests/test_diagnostic_feature_gating.py tests/test_lab_tests_admin.py -q | 113 passed, 1 skipped in 0.77s |
| 2026-11-13 05:40 | DX-00 baseline | python -m pytest -q -rs (clean worktree at `94de8ae`) | 8443 passed, 5 skipped, 15 warnings in 293.84s |

## 10. Log
| Date (IST) | Entry |
|:-|:-|
| 2026-11-13 | **P0 + P1 VERIFIED** on `dx/p1-foundation` (commit `feat(dx): P0 baseline + P1 foundation (DX-00…DX-11)`, one commit for the whole phase rather than one per task). Baseline 8443/5/0 at `94de8ae`; F1–F16 and AUDIT symbols re-located, no deltas. Migration 125 (`clinics.dx_state`/`dx_settings`, `dx_sites` with composite branch FK and forced RLS, `dx_purging`, `dx_purge_clinic` v1, `dx_update_settings` CAS) + rollback + verify block + `verify_supabase_schema.sql` §19. `tenant.py` `dx_eligible`/`dx_access`; 14 DX permissions, 5 roles, presets, implied grants, `holds_dx_permission`; `app/services/dx.py` + `app/routers/dx.py` (7 routes); platform toggle (400 ineligible, provision, 409 preview, confirm → WIND_DOWN), preview route, purge order; `/admin/me` DX fields; `LOGISTICS_RUNNER` / `B2B_PARTNER` confinement; LIS and G1 worklist scopes widened; `dx_sites` in `TENANT_OWNED_TABLES`; matrix counts; `test_dx_no_llm.py`; DX card in `platform.html`; DX Setup page in `admin/index.html`. Deviations in §7. Existing tests edited only where the change is the point: sidebar page order (+`dxsetup`), Ketu `PAGES` (+`dxsetup`, `app/assistant/registry.py`), the staff-permission regex in `test_held_report_recovery_and_staff_delete.py` widened to `[A-Z0-9_]+` (`DX_B2B_MANAGE` has a digit), `unwind_124` unwinds 125 first. Not applied to Supabase, not pushed. |
| 2026-11-05 | Forensic re-audit against code (AUDIT.md A1–A29) after a second owner brief (insurance/TPA patients must not receive reports; unpaid bills must never release reports; MocDoc Print Barcode dialog). Added DELIVERY-POLICY.md and ADR-DX-19…22; corrected barcode format (visit-shared sequence), per-target delivery ids, booking linkage (one test per booking, deposits), DX bill-line guard, B2B subjects without phone, G1/LIS/generic-resend guards, phlebotomist route names, voice/WhatsApp facts, plan eligibility table, field collections, label print dialog. Still no code. |
| 2026-11-05 | Document set authored (no code): competitive research (MocDoc screenshots + public pages for MocDoc/CrelioHealth; direct fetch blocked by network policy), gap analysis against `main` @ `56f719f`, architecture, 18 ADRs, data model DX-M1…M9, workflows W1–W18, report/template/label spec, API, UI, security, test plan, rollout, assistants, agent prompts. Baseline suite not run in the authoring session (no pytest installed). |

## 11. Next exact action
Owner: review/merge `dx/p1-foundation`; apply `125_dx_os_foundation.sql` to Supabase **before** deploying it (ROLLOUT §0.4).
Agent: start **DX-12** (migration DX-M2 as `126_dx_catalogue.sql`, re-check the number first) with the master prompt in
[AGENT-PROMPT.md](AGENT-PROMPT.md) §1. Lane PURE (DX-13 formula engine) may start in parallel.

## 12. Release decision
**P1 foundation built, not released.** Only the owner toggle, sites, settings and the setup page exist. The module stays off for every clinic until P11 is VERIFIED and the
owner switches it on per clinic, then per branch delivery owner (ROLLOUT §2).
