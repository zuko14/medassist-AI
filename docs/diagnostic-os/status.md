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
| Active phase | **P6b / P7** — OPD/IPD orders on the DX bench; reports, templates, delivery (P0–P4 VERIFIED 2026-11-13, P5–P6 VERIFIED 2026-11-14) |
| Next task | **DX-41** (P6b) or **DX-42** (P7, migration DX-M7, next free number 131) |
| Working branch | `dx/p1-foundation` (from `main` @ `94de8ae`; P1 `224e902`, P2 `2fec764`, P3 `ee6a219`, P4 `9c3cd66`, P5 `8acb461`, P6 on top; not pushed, no PR) |
| Last commit touching DX | `feat(dx): P6 bench, verification, approval (DX-36…DX-40)` on `dx/p1-foundation` |
| Last VERIFIED gate | P6 DG1–DG7 (§9, 2026-11-14) |
| Open blockers | none for code. Migrations 125–130 are **not applied** to Supabase (owner action, ROLLOUT §0.4: apply before deploying this code — `get_family_members` reads the 127 column) |
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
| P2 | Catalogue: departments, containers, signatories, test master, parameters, formulas, seed, approval | DX-12–17 | P1 | Lane PURE | DX-M2 | DG1–DG7 | VERIFIED |
| P3 | Registration, visits, orders (`source=dx`), billing, booking conversion | DX-18–25 | P2 | — | DX-M3 | DG1–DG7 | VERIFIED |
| P4 | Samples, labels, collection (desk + home), rejection, same-site receipt | DX-26–30 | P3 | P8a | DX-M4 | DG1–DG7 | VERIFIED |
| P5 | Logistics: routing, batches, hub receipt, outsourcing | DX-31–35 | P4 | P6, P8a | DX-M5 | DG1–DG7 | VERIFIED |
| P6 | Bench: stage worklists, entry with formulas/delta, verification, approval (single/batch), critical calls | DX-36–40 | P4 | P5, P8a | DX-M6 | DG1–DG7 | VERIFIED |
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
| DX-M2 | `126_dx_catalogue.sql` (+ `rollback/126_rollback_dx_catalogue.sql`) | `dx/p1-foundation` (not merged) | no | no |
| DX-M3 | `127_dx_visits_and_billing.sql` (+ `rollback/127_rollback_dx_visits_and_billing.sql`) | `dx/p1-foundation` (not merged) | no | no |
| DX-M4 | `128_dx_collection.sql` (+ `rollback/128_rollback_dx_collection.sql`) | `dx/p1-foundation` (not merged) | no | no |
| DX-M5 | `129_dx_logistics.sql` (+ `rollback/129_rollback_dx_logistics.sql`) | `dx/p1-foundation` (not merged) | no | no |
| DX-M6 | `130_dx_result_workflow.sql` (+ `rollback/130_rollback_dx_result_workflow.sql`) | `dx/p1-foundation` (not merged) | no | no |
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
| DX-12 | Migration DX-M2 (departments, containers, signatories, `lab_tests` + `lab_test_parameters` additive columns incl. `low/high_exclusive`, components + guard, approval-clearing trigger) | P1 | existing lab-test admin and WhatsApp catalogue tests pass unedited | `test_migration_dx_m2.py`, `test_lab_tests_*.py` | VERIFIED |
| DX-13 | `app/services/dx_formula.py` (Lane PURE) | — | all REPORT-TEMPLATES §1.1/§3 vectors | `test_dx_formula.py` | VERIFIED |
| DX-14 | `app/services/lis.py`: `flag_value` honours `low/high_exclusive` with False defaults; pure `delta_check`, `auto_verify_eligible` in `dx_results.py` | DX-12 | 118 flag vectors unchanged | `test_dx_flags.py`, DG5 | VERIFIED |
| DX-15 | Catalogue routes (API P2): departments, containers, signatories + signature upload, catalog list/edit, components, approve (digest CAS), formula check; LIS parameter routes accept DX fields and validate formulas | DX-12, DX-13 | | `test_dx_catalogue_api.py` | VERIFIED |
| DX-16 | Seed library `app/assets/dx_seed/tests_v1.json` (~150 tests, indicative bands marked) + seed preview/apply + CSV/XLSX parameter import with dry run | DX-15 | drafts inactive & unapproved; idempotent | `test_dx_catalogue_api.py` | VERIFIED |
| DX-17 | UI **Test Master** page | DX-15 | live UI | manual | VERIFIED |

**P2 DoD:** a lab director can seed/import, edit bands/formulas, preview calculations, and approve tests; WhatsApp
booking of the same tests unchanged.

### P3 — Registration, visits, billing (DX-M3)
| ID | Task | Depends | Acceptance | Tests | State |
|:-|:-|:-|:-|:-|:-|
| DX-18 | Migration DX-M3 (referrers, b2b accounts identity, accessions, items, samples table, events, `opd_orders` extension, invoice links, line guard, functions `dx_register_visit` / `dx_add_items` / `dx_cancel_item` / `dx_cancel_visit` / `dx_bill_visit`, purge v2) | P2 | ADR-DX-03 constraints; 115 suite unedited | `test_migration_dx_m3.py`, DG5 | VERIFIED |
| DX-19 | Guard every existing path against DX rows (AUDIT A4, A10, A20, A22): G1 worklist `source IN ('opd','ipd')`; G1 `collect_sample` / `complete_order` refuse `source='dx'` (409 `dx_use_dx_route`); DX bill-line guard trigger (OPD draft save on a DX bill refused, mapped message in `opd_billing`); generic Lab Reports resend / resolve refuse `source='dx'` rows; lab-test delete refuses a test ordered in DX | DX-18 | OPD/IPD behaviour identical | `test_dx_existing_paths.py`, DG5 | VERIFIED |
| DX-20 | `app/services/dx.py`: price quote, register (pricing in SQL), add/cancel items, visit read model with field filtering, timeline | DX-18 | W1/W2 steps 1–3 | `test_dx_visits_e2e.py` | VERIFIED |
| DX-21 | Routes: patient search/register (registry service under `_dx_scope`), referrers, B2B identity CRUD incl. payer kinds and the payer-routed CHECK, visits CRUD (API P3); B2B subjects without own phone → holder-patient dependants with `family_members.dx_b2b_account_id`; `get_family_members` hides them (ADR-DX-21) | DX-20 | conversation family tests unedited | `test_dx_visits_e2e.py`, `test_dx_b2b_subjects.py`, `test_dx_existing_paths.py` | VERIFIED |
| DX-22 | Billing glue: bill/issue/receipts/refunds/payment link via `opd_billing`; invoice `appointment_id` NULL; `_apply_prepaid` once per linked booking; `fee` items; supplementary bill for items added after issue; bill PDF A5 + 80 mm | DX-20 | Σ live invoices = Σ patient shares; each prepaid once | `test_dx_visits_e2e.py`, `test_dx_bookings.py` | VERIFIED |
| DX-23 | Today's bookings list + convert (all of a patient's bookings for the day → one visit via `dx_accession_bookings`; prepaid applied per booking) | DX-22 | W3 | `test_dx_visits_e2e.py` | VERIFIED |
| DX-24 | `data_retention.py` DX section | DX-18 | | `test_dx_data_retention.py` | VERIFIED |
| DX-25 | UI **Registration** page (incl. Today's bookings) | DX-21–23 | live UI, keyboard flow | manual | VERIFIED |

**P3 DoD:** W1 steps 1–3, W2, W3, W12 run end-to-end on the sandbox; concurrency cases 1–2 (TEST-PLAN §3) pass.

### P4 — Samples and collection (DX-M4)
| ID | Task | Depends | Acceptance | Tests | State |
|:-|:-|:-|:-|:-|:-|
| DX-26 | Migration DX-M4 (rejection reasons, sample guard, collect / bind / reject / receive-same-site / store functions) | P3 | ADR-DX-05 | `test_dx_samples.py` | VERIFIED |
| DX-27 | `app/services/dx_barcode.py` Code128 B/C (Lane PURE = DX-27a) + label PDF + ZPL in `dx_pdf.py` | — | published vectors | `test_dx_labels.py` | VERIFIED |
| DX-28 | Routes: collection queue, label print dialog (per sample / per test / register-book label, reprint log), bind, collect, reject (+ recollection WhatsApp: in-session or `dx_recollection_required` template), receive same-site, track sample | DX-26, DX-27 | W1 steps 4–6, W7; barcode vectors of REPORT-TEMPLATES §6 | `test_dx_samples.py`, `test_dx_labels.py` | VERIFIED |
| DX-29 | Home collection: `prepare-route`, phlebotomist samples + field-collection routes added to `_PHLEBOTOMIST_ROUTES`, portal Samples / Cash collected panel, cashier handover (`dx_field_collections`) | DX-28 | W4; phlebotomist still confined; handover creates exactly one receipt | `test_dx_samples.py`, `test_home_collection.py` unedited | VERIFIED |
| DX-30 | UI **Sample Collection** page | DX-28 | live UI, scanner + camera | manual | VERIFIED |

### P5 — Logistics and outsourcing (DX-M5)
| ID | Task | Depends | Acceptance | Tests | State |
|:-|:-|:-|:-|:-|:-|
| DX-31 | Migration DX-M5 | P4 | one live batch per tube; tie refused; guarded rollback | `test_migration_dx_m5.py` | VERIFIED |
| DX-32 | Routing rules CRUD + resolution inside `dx_register_visit` (`CREATE OR REPLACE`) + preview route | DX-31 | precedence; tubes split by destination | `test_dx_logistics.py`, `test_migration_dx_m5.py` | VERIFIED |
| DX-33 | Batches (create/add/dispatch/receive), stability timers (pure), `LOGISTICS_RUNNER` nav, `dx_sample_stability_scan` job | DX-31 | W6 | `test_dx_logistics.py` | VERIFIED |
| DX-34 | Outsource partners + orders `outsource_partner_id` + "performed at" data for P7 | DX-32 | W13 (data part) | `test_dx_logistics.py` | VERIFIED |
| DX-35 | UI **Logistics** page | DX-33 | live UI | manual | VERIFIED |

### P6 — Bench, verification, approval (DX-M6)
| ID | Task | Depends | Acceptance | Tests | State |
|:-|:-|:-|:-|:-|:-|
| DX-36 | Migration DX-M6 (meta, verifications, critical events, attachments, **gate trigger**, `dx_record_results`, `dx_verify_result`, `dx_approve_results`) | P4 | gate ignores OPD/IPD | `test_migration_dx_m6.py` (gate ignores OPD: `test_gate_ignores_opd_results`) | VERIFIED |
| DX-37 | LIS: skip `deliver_report` for `source='dx'`; LIS entry/validate routes return `409 dx_use_dx_route` for dx orders; `get_review_queue` excludes DX orders (AUDIT A5) | DX-36 | F4 closed; OPD/IPD unchanged | `test_dx_results_e2e.py`, DG5 | VERIFIED |
| DX-38 | `app/services/dx_results.py`: entry (formulas, delta, meta, critical detection), verify/return, approve single + batch, critical-call records, attachments | DX-36, DX-13, DX-14 | W8, W9 | `test_dx_results_e2e.py` | VERIFIED |
| DX-39 | Stage read model `dx_stage()` + `/worklist` + TAT (two clocks) + `dx_tat_breach_scan` job | DX-38 | ARCHITECTURE §4.2 | `test_dx_results_e2e.py` | VERIFIED |
| DX-40 | UI **Lab Worklist**, **Worksheet** drawer, **Approval** page | DX-38, DX-39 | live UI | manual | VERIFIED |
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
| 2026-11-13 | DX-18 | `opd_invoices.accession_id` and `opd_invoice_items.dx_accession_item_id` are plain references, not FKs; the DX line guard checks them | with FKs the clinic purge would be circular (DX first, but OPD bills would point into DX) | DATA-MODEL §4 "As built" |
| 2026-11-13 | DX-18 | Visit tables accept writes only inside `dx_*` functions (transaction flag, IPD pattern); one resolver `dx_price_items` prices and explodes for register, add-items and the new `dx_quote` (no write); same-key retries serialise on an advisory lock; both counters are taken after all validation | pricing and numbering can never be bypassed or diverge from the quote; AUDIT A13 | DATA-MODEL §4 |
| 2026-11-13 | DX-18 | `b2b_*` billing modes raise `dx_b2b_not_enabled` until DX-M8; accounts work now for identity, routing and subjects (billing `patient` or `internal`). Price: branch row → clinic row (rate cards in DX-M8); booking lines at the current price with `price_source='booking'`, the online payment applied as prepaid; home-collection fee from the booking as a fee line. Processing site: the registration site if it processes, else the clinic's only processing site (routing rules in DX-M5). Barcodes always generated (pre-printed / external labels in DX-M4); the visit number prefix is the fixed `LAB-` of the generated column (the `accession_prefix` setting is not used yet) | per DATA-MODEL §4 function notes and the owner brief | DATA-MODEL §4 |
| 2026-11-13 | DX-18 | New setting `max_staff_discount_percent` (default 10); a DX_ADMIN holder (admins included) may discount 100 % | "discount bounded by the actor's limit" had no limit defined | — |
| 2026-11-13 | DX-19 | The lab-test delete refusal is a database trigger (`trg_dx_lab_test_delete` → `dx_test_ordered`) mapped to 409 by both admin routes, not a query in the route | covers every delete path; the existing bulk-delete test drives the route against an in-memory `lab_tests` fake that a second query would break. Bulk delete runs in chunks: earlier chunks may already be deleted when a later chunk hits a DX-ordered test | — |
| 2026-11-13 | DX-19 | `get_family_members` drops B2B dependants in Python (`dx_b2b_account_id` set), not with `.is_()` in the query; the generic resend refusal lives in `LabReportService.resend_report` (`DxReportResendRefused` → 409) rather than a pre-query in the route | an unedited mock-based test pins the query chain; Python filtering also keeps the WhatsApp family picker working if code deploys before 127; the service guard covers voice and every other resend caller | — |
| 2026-11-13 | DX-19 | LIS entry / validate routes do **not** refuse DX orders yet (that is DX-37, P6); the G1 desk hides DX orders, so only a direct API call reaches them | task split as planned | — |
| 2026-11-13 | DX-21 | Catalogue reads (`/tests`, `/containers`, `/departments`, `/signatories`) also allow DX_REGISTER and DX_BILLING | the desk sells from the catalogue; found in the live check | API.md P2 |
| 2026-11-13 | DX-21 | Idempotency keys travel in the request body (as on OPD/IPD routes), not an `Idempotency-Key` header; B2B subjects are created by `POST /b2b-accounts/{id}/subjects` | house convention; the subject route was needed for ADR-DX-21 | — |
| 2026-11-13 | DX-22 | After issue, cancelling a test returns `dx_refund_first` until the bill is refunded and voided; the remaining tests then go on a new draft. The bill print is a browser print of a summary; the A5 / 80 mm bill **PDF is not built** (it needs the DX PDF renderer, DX-43) | W12 "refund first"; renderer belongs to P7 / Lane PURE | — |
| 2026-11-13 | DX-23 | Converting bookings stamps `appointments.checked_in_at` (status unchanged); a visit's source is `home_collection` if any booking is a home collection, `voice_booking` if any came by voice, else `whatsapp_booking` | W3 step 4 | — |
| 2026-11-13 | DX-24 | Erasure redacts a visit's name, DOB, external reference and delivery preferences (`dx_redact_patient`); clinical notes, tests, tubes, orders and bill lines stay | NMC clinical retention, GST records | — |
| 2026-11-13 | DX-12 | Column names follow the owner's P2 brief, not the DATA-MODEL sketch: `approved_at` / `approved_by` (doctor id) / `approval_digest`, `parameter_code`, `decimal_places`, `rounding_mode`, `delta_direction` / `delta_limit_percent` / `delta_limit_abs` / `delta_lookback_days`; `section_heading`, `is_reportable`, `comment` not added | owner brief is newer and explicit | DATA-MODEL §3 "As built" |
| 2026-11-13 | DX-12 | Approval digest and CAS live in SQL (`dx_test_digest`, `dx_approve_test` locking the test row); band / component triggers lock the test row `FOR NO KEY UPDATE` even when it is not approved | the 10-round race test caught a stale approval when an edit ran on an unapproved test while it was being approved; `NO KEY` avoids deadlocks with FK `KEY SHARE` locks | DATA-MODEL §3 |
| 2026-11-13 | DX-13 | Formula grammar = REPORT-TEMPLATES §3 (adds `sqrt`, `ln`, `log10`, `pow`, `**` ≤ 4, `iif`, `egfr_ckd_epi_2021`, patient context names), a superset of the brief's whitelist; eGFR pinned at 91.69 (male, Scr 1.0, 50 y) and 89.71 (female, 0.8, 50 y) | the doc's "90 ± 1" for the male vector was an estimate; the published equation (and the NKF calculator) give 92 | — |
| 2026-11-13 | DX-15 | Routes follow the owner's P2 brief (`/admin/dx/departments`, `/containers`, `/signatories`, `/tests…`, `/formula/check`, `/seed/*`, `/import/*`) instead of API.md's `/catalog/…`; band editing is `POST/PUT /tests/{id}/parameters` with DX fields; the LIS parameter routes are not extended (their updates leave DX columns untouched) | brief is explicit; one band editor with formula validation | API.md P2 rewritten as built |
| 2026-11-13 | DX-15 | Signature PNGs go to the existing private `lab-reports` bucket under `<clinic>/dx-assets/signatures/<sha256>.png`, not a new `dx-assets` bucket | a new bucket is a Supabase action (owner); same privacy, clinic prefix | API.md P2 |
| 2026-11-13 | DX-15 | Approval needs a doctor-linked login whose doctor has a registration number (DX_CATALOGUE_APPROVE alone is not enough), plus readiness: test → department, container (parameter mode), ≥ 1 active band, valid formulas; panel/package → ≥ 1 member | SECURITY §3.1 (an administrator cannot sign) and REPORT-TEMPLATES §1 rules | — |
| 2026-11-13 | DX-16 | Seed has 148 entries (132 tests, 11 panels, 5 packages), 8 departments, 11 containers; LOINC only where certain; every band indicative. `price_list_parser` not reused (it imports the AI engine); own CSV/XLSX reader. Apply is a sequence of bulk inserts, not one transaction: an interrupted apply is completed by re-running it (idempotent by `test_code`) | zero-LLM boundary (ADR-DX-10) | — |
| 2026-11-13 | DX-17 | Nav gating is `data-dx="nav DX_CATALOGUE DX_CATALOGUE_APPROVE"` (the P1 `applyDxGates` convention) rather than `perm:DX_CATALOGUE`; departments and containers are managed on the Test Master page; signatories have API only (no UI yet; the DX Setup checklist counts them) | keeps one gating rule; signatory UI belongs with report signing (P6/P7) | — |
| 2026-11-13 | DX-11 | A "Diagnostics" sidebar group already existed (Session 52, LIS / worklist / radiology for OPD+IPD). DX Setup is a `data-dx` link inside it, not a new group gated by `dx_access` | gating the existing group by `dx_access` would hide the lab pages from every OPD/IPD clinic; group visibility already follows its visible links | UI.md §1 unchanged in intent |
| 2026-11-13 | DX-26 | `dx_guard_sample` is a second trigger beside the 127 whole-row guard: it names the guarded columns, keeps the barcode immutable even inside `kriya.dx_tx`, and allows deletes only on purge. Helpers added: `dx_seed_rejection_reasons` (migration seeds live DX clinics; `provision_defaults` seeds on enable), `dx_sample_json`, `dx_visit_due`. Label prints are append-only (`dx_guard_append_only`); field collections are function-only (127 guard) with a UPI-reference CHECK | defence in depth; one source for "what is owed" shared by the collect gate and doorstep money | DATA-MODEL §5 "As built" |
| 2026-11-13 | DX-26 | `dx_collect_samples` accepts `id` or `sample_id`; a barcode sent for a labelled tube must match it (`dx_barcode_mismatch`, scan check), for an unbound pre-printed tube it binds; `collect_before_payment` counts doorstep money awaiting handover as paid **for collection only** (the report stays held, P7). Replay = a `collected` event with the same key. Rejection is allowed from collected / in transit / received / stored, refused once an order on the tube is resulted/completed or has a validated/amended LIS result; a double reject replays; a replacement of a pre-printed / external tube waits for its new label (no DX number consumed, AUDIT A8); `OTHER` needs a note | W1 failure paths, AUDIT A7/A8, ADR-DX-05 | DATA-MODEL §5 |
| 2026-11-13 | DX-27 | Labels live in `app/services/dx_barcode.py` (PDF via fpdf2 + ZPL), not `dx_pdf.py`; PDF bytes are deterministic (fixed creation date) | owner brief names `dx_barcode.py`; `dx_pdf.py` arrives with the report renderer (DX-43) | REPORT-TEMPLATES §6 unchanged |
| 2026-11-13 | DX-28 | Routes follow the owner's P4 brief: `POST /visits/{id}/labels/print` (body: `sample_ids`, `mode`, `format`, `copies`, `register_label`) instead of `GET labels.pdf/zpl` + `label-options`; `POST /samples/receive-direct`; `GET /samples/{barcode}/track`; `GET /rejection-reasons` (no POST/PATCH yet). `dx_mark_sample_stored` has no route yet (bench, P6). The queue also returns the caller's sites (a DX_COLLECT login cannot read `/sites`). The recollection notice is sent off the request path: in-session text, else the `dx_recollection_required` template (must be approved in Meta); B2B visits go to the account desk | brief is explicit; the notice must never fail or slow a rejection | API.md P4 as built |
| 2026-11-13 | DX-29 | Doorstep cash and home tubes are `/admin/dx/home-collection/visits/{appointment_id}/doorstep-cash` and `…/samples` (GET + POST) keyed by the **booking** (the phlebotomist's own assignment), resolved to its visit via `dx_accession_bookings`; cashier list `GET /home-collection/field-collections`, handover `POST /home-collection/field-collections/{id}/handover`. **Not built**: `prepare-route` (visits are created by *Today's bookings → Register*), the phlebotomist portal's Samples / Cash panel UI (API only), and the dispute action | owner P4 brief scoped DX-29 to the doorstep cash + handover routes and the confinement | API.md P4 |
| 2026-11-13 | DX-30 | Nav gating `data-dx="nav DX_COLLECT"` (P1 convention, as DX-17) instead of `perm:DX_COLLECT`. Scanning a code that is not waiting in the queue looks it up (`/track`) and opens Reject when the tube is collected / received — otherwise a fully collected visit leaves the queue and its tubes could not be rejected from this page (found in the live check) | one gating rule; Reject reachable after collection | — |
| 2026-11-14 | DX-31 | `dx_samples.outsource_partner_id` added (CHECK: at most one of processing site / partner); all new FKs are composite `(clinic_id, …)`; `dx_insert_lines` and `dx_reject_sample` replaced (rollback restores the 127 / 128 bodies). A tube not drawn at the batch's site is `dx_batch_wrong_origin`; a tube rejected while in a batch is receipted `damaged`; a partner batch is "received" (handed over) by its sending site with `received_site_id` NULL; manifest `created_at` is `clock_timestamp()` (scan order) | a tube must carry its destination for grouping, the batch check and recollection; the brief's `damaged` count had no other source | DATA-MODEL §6 "As built" |
| 2026-11-14 | DX-32 | Precedence lives only in SQL (`dx_resolve_routing`); `dx.resolve_routing` and the preview route call it. Within a level a rule naming the sending site beats an any-site rule, then priority. Ties = a partial unique index. Rule / partner writes are clinic-wide (a pinned DX_ADMIN gets 403); DELETE removes the rule (audited) | one resolver for registration, preview and service; the brief's "lowest priority breaks ties" left site-specific vs any-site open | API.md P5 |
| 2026-11-14 | DX-33 | Routes follow the owner's P5 brief (`GET /batches?from_site&to_site&status`, `POST /batches/{id}/samples`, no `routing/resolve`, no sample removal); `LOGISTICS_RUNNER` confinement widened to `/batches…`, `GET /outsource-partners`, `GET /samples/{barcode}/track`; `GET /batches` also returns sites and partners (a runner cannot read `/sites`). Stability: cold window when the batch's dispatch temperature ≤ 8 °C, else room hours; the scan job logs (`DX_SAMPLE_STABILITY`) — no dashboard notice yet. Visit reads strip `external_ref` / `delivery_prefs` without a desk grant and both contact and money for a pinned user reaching another branch's visit through a sample site | brief is explicit; SECURITY §4 table | API.md P5 |
| 2026-11-14 | DX-34 | "Outsourced to <partner>" is on visit orders (`outsourced_to`) and collection-queue tubes; the DX bench worklist arrives with P6 | no DX test worklist exists before P6 | — |
| 2026-11-14 | DX-35 | Receipt warns inline ("N not scanned will be recorded as missing") instead of a `confirm()`; a runner login lands on Logistics; nav gating `data-dx="nav DX_LOGISTICS DX_RECEIVE DX_ADMIN"` | found in the live check | — |
| 2026-11-14 | DX-36 | `dx_result_value_meta.parameter_id` is a reference, not an FK (as `lab_result_values`, 118: band rows can change while a recorded result stays); composite result FKs; meta / verifications / critical events written only in functions, the last two append-only; `is_auto` CHECKed false; a returned verification needs a comment. Added `dx_log_critical_call` (a call is a new row copying its detection) and `dx_critical_pending`; snapshots for `dx_approve_results` travel inside each item; `dx_record_results` allows a tube-less test without receipt and refuses a cancelled order | function-only writes keep the gate meaningful; "rows are never updated" (DATA-MODEL §7) needed a call function | DATA-MODEL §7 "As built" |
| 2026-11-14 | DX-37 | LIS 409 detail is the bare string `dx_use_dx_route` (as the brief), not the `{code, message}` shape; the review queue filters on the orders it already reads (now for every row, before paging) | brief is explicit; no extra query | API.md P6 |
| 2026-11-14 | DX-38 | Routes follow the owner's P6 brief (`/critical-events/{id}/call`, `/results/approve`), plus `GET /orders/{id}/worksheet` and `GET /results/{id}` (the drawer and the approval review need them) and `preview: true` on the save route (live calculated values / flags without saving). Previous value for the delta = latest validated result of the same test for the same person (other orders), matched by parameter name within its lookback. A DX signatory row, when present, adds designation / qualification / signature path to the signer snapshot; approval does not require one (doctor-linked login with a registration number does, as LIS). Attachments ≤ 5 MB (API.md said 2 MB), type sniffed | brief; PDFs from partners exceed 2 MB | API.md P6 |
| 2026-11-14 | DX-39 | `dx_stage(order, sample, result, visit, ctx)` takes the settings / verification / dues in `ctx` so it stays pure; `released` waits for P7 releases (every signed result is `pending_release` now). TAT clock follows `tat_clock` (both clocks also returned); the clock stops at sign-off. `/worklist` takes `open_only` (open visits of 7 days) besides `date`; the TAT scan logs `DX_TAT_BREACH` (no dashboard notice yet) and skips a failing clinic | P7 owns releases | API.md P6 |
| 2026-11-14 | DX-40 | Pages `dxbench` (Lab Worklist) and `dxapprove` (Result Approval); the worksheet is a drawer on both (`dxws`), not its own page. Bench uses its own `dxLab()` (LAB_ENTRY / LAB_VALIDATE on a DX clinic, the server's role map) because `lisCan` needs OPD or IPD. Found in the live check and fixed: technician saw a read-only worksheet (that check), the critical-call dialog opened behind the drawer, live evaluation dropped keyboard focus, a fast tab switch rendered a stale list, phone rows overlapped | — | — |

## 8. Known issues and limitations
- At authoring, migrations 115–124 (which DX builds on) are not applied to Supabase; DX cannot reach staging first.
- `admin/index.html` is a very large single file — UI lanes must use the DX markers (UI.md §1) to avoid conflicts.
- Re-audit 2026-11-05 ([AUDIT.md](AUDIT.md)) corrected the first draft in 29 places (2 critical: dues gating and payer
  routing were incomplete). Agents must treat AUDIT.md as binding where it differs from memory of the earlier draft.
- Approval requires doctor-linked logins with registration numbers; centres where non-doctor scientists sign
  reports under state rules need an owner decision (signatory eligibility is data in `dx_signatories`, but the 118
  CHECK requires a registration number).
- `deactivation_preview` still returns zeros (P3–P5 did not add their counts: visits in process, collected tubes,
  tubes in transit); the owner's switch-off is not blocked by open visits yet.
- P5 not built: removing a tube from an open batch, cancelling a batch, re-batching or late-receiving a tube recorded
  missing, the discrepancy notice to the source site's dashboard (W6 step 3 — recorded on the batch and timeline only),
  stability alerts beyond the scheduler log.
- P6 not built: the referrer's "urgent result" template for a critical value (W8 step 6), instrument prefill (P10),
  a dashboard notice for TAT breaches (log only), a "Done" stage (needs P7 releases). Approval does not yet require a
  `dx_signatories` row for the test's department (any doctor-linked login with a registration number may sign, as in
  the LIS). Attachment files stay in storage after a purge.
- DX-29 remainder: `prepare-route`, the phlebotomist portal Samples / Cash panel UI and the field-collection dispute
  action (API and DB status exist). Home visits are registered from the desk's *Today's bookings* meanwhile.
- The `dx_recollection_required` WhatsApp template must be submitted to Meta by the owner; until approved, patients
  outside the 24 h window get no recollection message (the rejection itself is unaffected).

## 9. Verification evidence (paste command and summary line; newest first)
| Date (IST) | Task / gate | Command | Result |
|:-|:-|:-|:-|
| 2026-11-14 | P6 / DG7 | python -m pytest -q -rs | 8944 passed, 5 skipped, 15 warnings in 305.25s (P5 8898 + 46 new: 19 P6 tests + 27 matrix cases for the 9 new routes; same 5 skips) |
| 2026-11-14 | P6 / DG5 | python -m pytest tests/test_migration_115_orders.py tests/test_orders_api_e2e.py tests/test_migration_118_lis.py tests/test_lis_api_e2e.py -q | 33 passed |
| 2026-11-14 | DX-36…39 | python -m pytest tests/test_migration_dx_m6.py tests/test_dx_results_e2e.py -q | 19 passed (10 migration: entry + meta + event, receipt required, critical detection → call without read-back still pending → with read-back approvable, call rule off, three levels: self-verification refused / re-entry retires a verification / returned blocks, **gate ignores OPD results on a three-level clinic**, batch signs normal + refuses abnormal / unknown with per-item outcome + replay, attachments only while open, purge v6, guarded rollback + reapply twice; 9 e2e: worklist stages + TAT + LIS 409 + sign-off queue without DX, preview + calculated ratio, critical call before approval, three levels over HTTP, batch + delta from a previous validated value + signer rules, batch signs normals, attachments, pure stage / TAT, TAT scan) |
| 2026-11-14 | DX-36 rollback chain | python -m pytest tests/test_migration_dx_m1.py … test_migration_dx_m5.py tests/test_migration_115_orders.py tests/test_migration_118_lis.py tests/test_migration_103_opd.py tests/test_migration_111_ipd.py tests/test_migration_124_insurance.py tests/test_migration_121_emergency.py -q | 159 passed (`unwind_129` now unwinds 130 first) |
| 2026-11-14 | P6 / DG3 | python -m pytest tests/test_clinical_firewall.py tests/test_dx_no_llm.py -q | 36 passed |
| 2026-11-14 | P6 / DG2 | python -m pytest tests/test_admin_super_admin_scope_matrix.py tests/test_phase2_route_adversarial_matrix.py -q | 1386 passed (89 `/admin/dx` routes) |
| 2026-11-14 | P6 / DG1 | python -m pytest tests/test_lint_unscoped_queries.py tests/test_phase4_scoped_queries.py -q | 10 passed |
| 2026-11-14 | DX-40 live UI | real app + fresh pgserver (001→130 applied clean) + bridge; three-level lab; Asha (STAT Hb + lipid with calculated TC/HDL ratio, a validated Hb 13.1 from an earlier visit), Ravi (glucose 450 entered, critical), Meena / Kiran (normal glucose, verified) | **Tech @1440 px**: Lab Worklist tabs Receipt 0 · Entry 2 · Verification 1 · Approval 2 · Release 1; Hb worksheet: typed 9.8 → live "low", "Previous 13.1", "Delta 25.19%"; saved with the delta in meta; lipid 210 / 42 → ratio 5 computed server-side (typed value ignored), flags high / normal / normal; focus kept while typing. **Supervisor**: Ravi's worksheet shows the critical banner; call dialog (Record disabled until read-back ticked) → recorded; verified Ravi, Asha's two → Approval 5. **Pathologist**: Result Approval lists Ravi (critical) first; Normal results tab offers exactly Meena + Kiran; Select all → Sign selected → chips "✓ Meena Shah · Glucose", "✓ Kiran Rao · Glucose", toast "2 signed"; Ravi approved from the drawer → validated, signed "Dr. Rao". **@390 px**: drawer full width, page 390, no overflow. Console: no errors |
| 2026-11-14 | P5 / DG7 | python -m pytest -q -rs | 8898 passed, 5 skipped, 15 warnings in 304.12s (P4 8837 + 61 new: 19 P5 tests + 42 matrix cases for the 14 new routes). Skips: 3 × `KRIYA_TEST_LIVE`, 1 × MocDoc sandbox, 1 × missing `issues/ACCUMAX CHATBOT.csv` fixture (`test_lab_tests_admin.py:369`) |
| 2026-11-14 | P5 / DG5 | python -m pytest tests/test_migration_115_orders.py tests/test_orders_api_e2e.py tests/test_migration_118_lis.py tests/test_lis_api_e2e.py -q | 33 passed |
| 2026-11-14 | DX-31…34 | python -m pytest tests/test_migration_dx_m5.py tests/test_dx_logistics.py -q | 19 passed (11 migration: precedence, tie, split by destination, batch lifecycle with missing / damaged / unexpected, refusals, one-live-batch index + guard, recollection keeps partner, purge v5, guarded rollback + reapply twice; 8 e2e: partners / rules / preview, desk split + "Outsourced to", runner → hub receipt with discrepancy, batch site scope, processing-site user sees no phone / money, runner confinement, stability calculator, scan job) |
| 2026-11-14 | DX-31 rollback chain | python -m pytest tests/test_migration_dx_m1.py … test_migration_dx_m4.py tests/test_migration_115_orders.py tests/test_migration_118_lis.py tests/test_migration_103_opd.py tests/test_migration_111_ipd.py tests/test_migration_124_insurance.py tests/test_migration_121_emergency.py tests/test_home_collection.py tests/test_dx_visits_e2e.py tests/test_dx_samples.py -q | 248 passed (`unwind_128` now unwinds 129 first) |
| 2026-11-14 | P5 / DG3 | python -m pytest tests/test_clinical_firewall.py tests/test_dx_no_llm.py -q | 36 passed |
| 2026-11-14 | P5 / DG2 | python -m pytest tests/test_admin_super_admin_scope_matrix.py tests/test_phase2_route_adversarial_matrix.py -q | 1359 passed (80 `/admin/dx` routes) |
| 2026-11-14 | P5 / DG1 | python -m pytest tests/test_lint_unscoped_queries.py tests/test_phase4_scoped_queries.py -q | 10 passed |
| 2026-11-14 | DX-35 live UI | real app + fresh pgserver (migrations 001→129 applied clean) + bridge; seeded Kukatpally (collection) → Central Hub (processing), rule "every test → hub", "Vitamin D → Metro Reference Lab"; 3 collected visits | Meena's Lipid + Vitamin D (same serum container) registered as **two tubes**. **Runner @1440 px**: lands on Logistics (Routing tab hidden); New batch → TRN-2026-00001 to Central Hub, carrier + 22 °C; scanned 4 tubes — the Vitamin D tube refused "routed to a different lab than this batch: 260100000004"; stability dots: EDTA 3.99 h left (green), Ravi's EDTA 0.65 h left (amber); dispatch at 6 °C → serum switched to the 72 h cold window. **Hub tech**: Incoming shows the batch; Receive scanning 2 of 4 + a stray → "Received · discrepancy": 2 received, 2 missing (barcodes listed), 0 damaged, 1 not on manifest. Track 260700000001 → Registered → Collected → Dispatched in batch TRN-2026-00001 → Central Hub · 6 °C → Received at Central Hub. **Admin**: Routing & Partners lists partner (NABL MC-2210) and rules; added a department rule; a tying rule → toast "Another active rule … same priority"; preview "Vitamin D → Outsourced to Metro Reference Lab (a rule for this test)". **@390 px**: page width 390, no overflow, tube rows stack. Fixed in this check: receive list overflowing the modal, native `confirm()` for missing tubes. Console: no errors |
| 2026-11-13 | P4 / DG7 | python -m pytest -q -rs | 8837 passed, 5 skipped, 15 warnings in 966.59s (P3 8755 + 82 new; same 5 skips; slower run: a second pytest shared the pgserver part of the time) |
| 2026-11-13 | DX-26 rollback chain | python -m pytest tests/test_migration_dx_m1.py tests/test_migration_dx_m2.py tests/test_migration_dx_m3.py tests/test_migration_103_opd.py tests/test_migration_111_ipd.py tests/test_migration_124_insurance.py tests/test_migration_121_emergency.py tests/test_home_collection.py tests/test_dx_visits_e2e.py tests/test_dx_bookings.py tests/test_dx_feature_gate.py -q | 230 passed (`unwind_127` now unwinds 128 first; `test_home_collection.py` unedited) |
| 2026-11-13 | P4 / DG5 | python -m pytest tests/test_migration_115_orders.py tests/test_orders_api_e2e.py tests/test_migration_118_lis.py tests/test_lis_api_e2e.py -q | 33 passed |
| 2026-11-13 | DX-26…29 | python -m pytest tests/test_migration_dx_m4.py tests/test_dx_samples.py tests/test_dx_labels.py -q | 43 passed (13 migration incl. guarded rollback + reapply twice; 15 e2e incl. home tubes, doorstep cash, handover = 1 receipt, phlebotomist confinement; 15 label incl. Code128 vectors, PDF determinism, ZPL injection) |
| 2026-11-13 | P4 / DG3 | python -m pytest tests/test_clinical_firewall.py tests/test_dx_no_llm.py -q | 36 passed |
| 2026-11-13 | P4 / DG2 | python -m pytest tests/test_admin_super_admin_scope_matrix.py tests/test_phase2_route_adversarial_matrix.py -q | 1317 passed (66 `/admin/dx` routes) |
| 2026-11-13 | P4 / DG1 | python -m pytest tests/test_lint_unscoped_queries.py tests/test_phase4_scoped_queries.py -q | 10 passed |
| 2026-11-13 | DX-30 live UI | real app + real Postgres (pgserver + bridge), seeded lab with 3 visits (STAT fasting 3 tubes, urgent, routine); login DX_COLLECT front desk | **@1440 px**: Sample Collection in the Diagnostics nav; queue STAT (red) → Urgent → Routine, Fasting / Not fasting chips, colour dots, container names, barcodes, tests. Typed `260700000002` + Enter in the scan box (keyboard-wedge) → EDTA row ✓ highlighted; Collect → toast "3 tube(s) collected · LAB-2026-000002", visit left the queue. Print labels (per test + register) → 200 PDF. Scanned the collected tube again → Reject dialog ("…LAB-2026-000002 · Asha Reddy · collected"), Haemolysed + recollect → toast "New tube 260700000004 added", visit back in the queue with the old tube Rejected and the new one marked Recollection; server logged the recollection notice. **@390 px**: page width 390, no overflow, tube rows stack. Bugs found and fixed in this check: fasting badge missing (queue lacked `fasting_status`), chips wrapping, status cell squeezed on phones, Reject unreachable once a visit was fully collected. Console: only the existing dashboard 403s for a staff login |
| 2026-11-13 12:20 | P3 / DG7 | python -m pytest -q -rs | 8755 passed, 5 skipped, 15 warnings in 311.45s (P2 8655 + 100 new; same 5 skips) |
| 2026-11-13 12:05 | P3 / DG7 first run | python -m pytest -q -rs | 5 failed, 8750 passed: mock-based tests of the generic resend route (4) and `get_family_members` (1) broke on the added queries. Fixed by moving the DX resend refusal into `LabReportService.resend_report` (covers every caller) and filtering B2B dependants in Python; reran the 5 + DX path tests (31 passed), then the full run above |
| 2026-11-13 11:50 | P3 / DG5 | python -m pytest tests/test_migration_115_orders.py tests/test_orders_api_e2e.py tests/test_migration_118_lis.py tests/test_lis_api_e2e.py -q | 33 passed, 2 warnings in 2.62s |
| 2026-11-13 11:50 | DX-18…24 | python -m pytest tests/test_migration_dx_m3.py tests/test_dx_existing_paths.py tests/test_dx_visits_e2e.py tests/test_dx_bookings.py tests/test_dx_b2b_subjects.py tests/test_dx_data_retention.py -q | 37 passed, 2 warnings in 2.59s (incl. 10 parallel same-key registrations → 1 visit; 20 parallel desks → gapless visit and sample numbers) |
| 2026-11-13 11:50 | P3 / DG3 | python -m pytest tests/test_clinical_firewall.py tests/test_dx_no_llm.py -q | 36 passed, 1 warning in 0.21s |
| 2026-11-13 11:50 | P3 / DG2 | python -m pytest tests/test_admin_super_admin_scope_matrix.py tests/test_phase2_route_adversarial_matrix.py -q | 1278 passed, 2 warnings in 3.84s (53 `/admin/dx` routes) |
| 2026-11-13 11:50 | P3 / DG1 | python -m pytest tests/test_lint_unscoped_queries.py tests/test_phase4_scoped_queries.py -q | 10 passed, 1 warning in 21.44s |
| 2026-11-13 11:40 | DX-25 live UI | real app + real Postgres (pgserver + bridge), seeded lab (5 approved tests + a package, a referrer, a B2B account, 2 bookings for today, one prepaid ₹450) | **Desk (DX_REGISTER + DX_BILLING) @1440 px**: searched "Asha", picked her; referrer type-ahead "anur" → Dr. N. Anuradha; cart BASIC package (shows "4 tests: TSH, GLU_F, CBC, LIPID", 3 tube dots) + HbA1c with ₹45 discount → totals Price ₹1,949, Discount − ₹45, Patient pays ₹1,904 (from `/visits/quote`); Save & Bill → toast "Visit LAB-2026-000001 registered", tubes 260100000001 / 260800000001 / 260700000001 (one shared sequence), INV-2026-00001 issued ₹1,904; Take payment (cash) → "paid". Visit kept referrer snapshot and priority Urgent. **Today's bookings**: Ravi Kumar, HBA1C + TSH, paid online ₹450 → Arrived → Register → LAB-2026-000002, tubes 2607…02 / 2601…02, INV-2026-00002 partially paid (₹450 applied, ₹300 due); list then empty. After a save the form resets (no patient, priority Routine) — a bug found in this check and fixed. **@390 px**: page width 390, panels stacked, cart rows as cards. **DX_REGISTER only**: Save & Bill hidden, Save only works ("Bill now" offered). **LAB_OPERATOR**: DX Registration hidden. Console: no errors. The desk could not read the test list at first (catalogue reads lacked DX_REGISTER) — fixed server-side |
| 2026-11-13 11:20 | DX-18 rollback chain | python -m pytest tests/test_migration_dx_m1.py tests/test_migration_dx_m2.py tests/test_migration_115_orders.py tests/test_migration_118_lis.py tests/test_migration_103_opd.py tests/test_migration_111_ipd.py tests/test_migration_124_insurance.py tests/test_migration_121_emergency.py tests/test_orders_api_e2e.py tests/test_lis_api_e2e.py tests/test_lab_tests_bulk_delete.py tests/test_lab_tests_admin.py -q | 168 passed, 1 skipped after the 115 rollback test learned to unwind 127 first |
| 2026-11-13 08:00 | P2 / DG7 | python -m pytest -q -rs | 8655 passed, 5 skipped, 15 warnings in 305.85s (P1 8502 + 153 new; same 5 skips) |
| 2026-11-13 07:52 | P2 / DG7 first run | python -m pytest -q -rs | 3 failed, 8652 passed: `GET /admin/dx/setup` 500 after the checklist began counting signatories (`list_rows` ordered `dx_signatories` by a non-existent `code`). Fixed in `dx_catalogue.list_rows`, regression assertion added to `test_departments_and_containers`; DX files rerun 67 passed, then the full run above |
| 2026-11-13 07:35 | P2 / DG5 | python -m pytest tests/test_orders_api_e2e.py tests/test_lis_api_e2e.py -q | 17 passed, 2 warnings in 2.02s |
| 2026-11-13 07:35 | DX-12…16 | python -m pytest tests/test_migration_dx_m2.py tests/test_dx_formula.py tests/test_dx_flags.py tests/test_dx_catalogue_api.py -q | 78 passed, 2 warnings in 2.06s |
| 2026-11-13 07:35 | P2 / DG3 | python -m pytest tests/test_clinical_firewall.py tests/test_dx_no_llm.py -q | 36 passed, 1 warning in 0.20s |
| 2026-11-13 07:35 | P2 / DG2 | python -m pytest tests/test_admin_super_admin_scope_matrix.py tests/test_phase2_route_adversarial_matrix.py -q | 1215 passed, 2 warnings in 6.54s (32 `/admin/dx` routes) |
| 2026-11-13 07:35 | P2 / DG1 | python -m pytest tests/test_lint_unscoped_queries.py tests/test_phase4_scoped_queries.py -q | 10 passed, 1 warning in 21.49s |
| 2026-11-13 07:30 | DX-17 live UI | real app + real Postgres (pgserver + bridge), `diagstream` lab with DX on | **Lab director (PATHOLOGIST, doctor-linked) @1440 px**: Diagnostics group shows DX Setup + Test Master; Seed library preview "148 … 148 new", apply → toast "Added 148 tests (245 bands, 95 panel links)", list shows 148 with Not approved / Inactive chips, container swatches. Lipid Profile editor: details, 8 parameters with printed ranges and formulas; live formula check "✗ Unknown parameter code. (TGX)" then "✓ Valid. Uses HDL, TC, TG."; Approve → "Approved" chip; editing TC's high bound → toast "Band saved. The test needs approval again." and the approval cleared. Import bands: file with one unknown code → "2 row(s): 1 valid, 1 to fix" and Import disabled; clean file → enabled, "Imported 1 band(s)". **@390 px**: page width 390, list and band tables collapse to labelled cards, editor modal fits (382 px). **clinic_admin**: Test Master visible, approval refused 403 "Approval needs a login linked to a doctor with a registration number." **LAB_OPERATOR**: Test Master hidden. Console: no errors |
| 2026-11-13 07:05 | DX-12 regressions | python -m pytest tests/test_lab_tests_*.py tests/test_lab_test_*.py tests/test_migration_118_lis.py tests/test_lis_api_e2e.py tests/test_orders_api_e2e.py tests/test_migration_103_opd.py tests/test_migration_124_insurance.py -q | 211 passed, 1 skipped (lab-test admin, bulk delete, WhatsApp lab booking unedited; 118 rollback test now unwinds 126 first) |
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
| 2026-11-14 | **P6 VERIFIED** on `dx/p1-foundation` (commit `feat(dx): P6 bench, verification, approval (DX-36…DX-40)`). Migration 130: `dx_result_value_meta`, `dx_result_verifications` (append-only, human only), `dx_critical_events` (detection + calls, append-only), `dx_result_attachments` (open results only); gate `trg_dx_lab_results_gate` (DX only, three levels); `dx_record_results`, `dx_verify_result`, `dx_log_critical_call`, `dx_critical_pending`, `dx_approve_results` (per-item sub-transactions, batch = all normal); purge v6; rollback; verify §24. LIS: 409 `dx_use_dx_route` on entry / validate, sign-off queue without DX, no per-test delivery for DX. `dx_results.py` workflow (calculated parameters, flags, deltas, preview, signer snapshot, attachments), `dx_stage` / `tat_status` / worklist, `dx_tat_breach_scan` (5 min), 9 routes, Lab Worklist + worksheet drawer + Result Approval pages (DX:bench markers). Existing tests edited only for newest-first unwind (`unwind_129` → `unwind_130`) and sidebar order (+`dxbench`, `dxapprove`). Not applied to Supabase, not pushed. |
| 2026-11-14 | **P5 VERIFIED** on `dx/p1-foundation` (commit `feat(dx): P5 logistics and outsourcing (DX-31…DX-35)`). Migration 129: `dx_outsource_partners`, `dx_routing_rules` (tie index), `dx_batches` (`TRN-`), `dx_batch_samples` (one live batch per tube), `opd_orders` / `dx_samples.outsource_partner_id`; `dx_resolve_routing`, `dx_insert_lines` + `dx_reject_sample` replaced (split by destination, partner kept), `dx_create_batch`, `dx_batch_add`, `dx_dispatch_batch`, `dx_receive_batch`, `dx_batch_json`; purge v5; rollback; verify §23. Service: routing preview, partners / rules CRUD, batches, pure stability calculator, `dx_sample_stability_scan` (10 min, `scheduler_locks`); 14 routes; runner confinement widened; visit read model minimised for processing-site logins; Logistics page (DX:logistics markers). Existing tests edited only for newest-first unwind (`unwind_128` → `unwind_129`) and sidebar order (+`dxlogistics`). Deviations in §7. Not applied to Supabase, not pushed. |
| 2026-11-13 | **P4 VERIFIED** on `dx/p1-foundation` (commit `feat(dx): P4 samples and collection (DX-26…DX-30)`). Migration 128: `dx_rejection_reasons` (11 defaults, seeded on enable), `dx_field_collections` (pending_handover → handed_over with the receipt), `dx_label_prints` (append-only), `dx_guard_sample` (barcode immutable, columns only in functions), `dx_bind_barcode`, `dx_collect_samples`, `dx_reject_sample` (cancel + replacement tube/orders, refused once signed), `dx_receive_sample` (same site), `dx_mark_sample_stored`, `dx_record_field_collection`, `dx_handover_field_collection`, `dx_visit_due`; purge v4; rollback; verify §22. `dx_barcode.py` (Code128 B/C, 50×25 mm PDF, ZPL, register label), 13 routes (queue, labels, bind, collect, reject + recollection notice, receive-direct, track, reasons, home tubes, doorstep cash, field-collection list, handover), phlebotomist confinement widened by 3 routes, Sample Collection page (DX:samples markers). Existing tests edited only for newest-first unwind (`unwind_127` → `unwind_128`), sidebar order (+`dxcollect`), matrix floors (66). Deviations in §7. Not applied to Supabase, not pushed. |
| 2026-11-13 | **P3 VERIFIED** on `dx/p1-foundation` (commit `feat(dx): P3 registration, visits, billing (DX-18…DX-25)`). Migration 127: referrers, B2B account identity (payer-routed CHECK), visits, bookings link, items (shares CHECK), tubes, timeline; `opd_orders` `source='dx'` (doctor optional only for dx, shape branch, explode-once index); DX bill link + line guard; write guards; lab-test delete guard; `dx_price_items`, `dx_register_visit`, `dx_quote`, `dx_add_items`, `dx_cancel_item`, `dx_cancel_visit`, `dx_bill_visit`, `dx_redact_patient`; purge v3; rollback; verify §21. G1 worklist / collect / complete, OPD draft save, generic resend / resolve and lab-test delete guarded; `get_family_members` hides B2B dependants. `dx.py` visit service, `dx_billing.py` (issue, prepaid once per booking, receipts with replay, payment link), 21 new routes, data-retention step, DX Registration page with Today's bookings. Existing tests edited only for newest-first unwinds (115, 118), sidebar order and Ketu page table (+`dxdesk`), matrix counts. Deviations in §7. Not applied to Supabase, not pushed. |
| 2026-11-13 | **P2 VERIFIED** on `dx/p1-foundation` (commit `feat(dx): P2 catalogue (DX-12…DX-17)`). Migration 126: `dx_departments`, `dx_containers` (2-digit codes), `dx_signatories`, `dx_test_components`; DX columns on `lab_tests` / `lab_test_parameters`; component / kind guards; approval digest + CAS (`dx_test_digest`, `dx_approve_test`) with triggers that clear approval on any clinical edit and lock the test row; purge v2; rollback restores purge v1; verify §20. `dx_formula.py` (ast whitelist, Decimal, CKD-EPI 2021, cycles), `lis.flag_value` exclusive bounds (118 vectors unchanged), `dx_results.py` (delta check, auto-verify eligibility), `dx_catalogue.py` + 25 catalogue routes, `dx_seed.py` + `tests_v1.json` (148 entries) + CSV/XLSX band import with dry run, Test Master page. Existing tests edited only for newest-first unwinds (118), sidebar order / Ketu page table (+`dxcatalog`), matrix counts. Deviations in §7. Not applied to Supabase, not pushed. |
| 2026-11-13 | **P0 + P1 VERIFIED** on `dx/p1-foundation` (commit `feat(dx): P0 baseline + P1 foundation (DX-00…DX-11)`, one commit for the whole phase rather than one per task). Baseline 8443/5/0 at `94de8ae`; F1–F16 and AUDIT symbols re-located, no deltas. Migration 125 (`clinics.dx_state`/`dx_settings`, `dx_sites` with composite branch FK and forced RLS, `dx_purging`, `dx_purge_clinic` v1, `dx_update_settings` CAS) + rollback + verify block + `verify_supabase_schema.sql` §19. `tenant.py` `dx_eligible`/`dx_access`; 14 DX permissions, 5 roles, presets, implied grants, `holds_dx_permission`; `app/services/dx.py` + `app/routers/dx.py` (7 routes); platform toggle (400 ineligible, provision, 409 preview, confirm → WIND_DOWN), preview route, purge order; `/admin/me` DX fields; `LOGISTICS_RUNNER` / `B2B_PARTNER` confinement; LIS and G1 worklist scopes widened; `dx_sites` in `TENANT_OWNED_TABLES`; matrix counts; `test_dx_no_llm.py`; DX card in `platform.html`; DX Setup page in `admin/index.html`. Deviations in §7. Existing tests edited only where the change is the point: sidebar page order (+`dxsetup`), Ketu `PAGES` (+`dxsetup`, `app/assistant/registry.py`), the staff-permission regex in `test_held_report_recovery_and_staff_delete.py` widened to `[A-Z0-9_]+` (`DX_B2B_MANAGE` has a digit), `unwind_124` unwinds 125 first. Not applied to Supabase, not pushed. |
| 2026-11-05 | Forensic re-audit against code (AUDIT.md A1–A29) after a second owner brief (insurance/TPA patients must not receive reports; unpaid bills must never release reports; MocDoc Print Barcode dialog). Added DELIVERY-POLICY.md and ADR-DX-19…22; corrected barcode format (visit-shared sequence), per-target delivery ids, booking linkage (one test per booking, deposits), DX bill-line guard, B2B subjects without phone, G1/LIS/generic-resend guards, phlebotomist route names, voice/WhatsApp facts, plan eligibility table, field collections, label print dialog. Still no code. |
| 2026-11-05 | Document set authored (no code): competitive research (MocDoc screenshots + public pages for MocDoc/CrelioHealth; direct fetch blocked by network policy), gap analysis against `main` @ `56f719f`, architecture, 18 ADRs, data model DX-M1…M9, workflows W1–W18, report/template/label spec, API, UI, security, test plan, rollout, assistants, agent prompts. Baseline suite not run in the authoring session (no pytest installed). |

## 11. Next exact action
Owner: review/merge `dx/p1-foundation`; apply migrations 125–130 to Supabase **before** deploying it (ROLLOUT §0.4);
submit the `dx_recollection_required` WhatsApp template.
Agent: start **DX-41** (P6b, OPD/IPD orders on the DX bench) or **DX-42** (P7, migration DX-M7 as `131_…`, re-check
the number first) with the master prompt in [AGENT-PROMPT.md](AGENT-PROMPT.md) §1. Lane PURE (DX-43a PDF renderer — also needed for the A5 / 80 mm bill) may start
in parallel; DX-27a (barcode) is done.

## 12. Release decision
**P1–P6 built, not released.** Owner toggle, setup, test master with approval, registration, billing, booking
conversion, tube labels, collection (desk and home), rejection with recollection, same-site receipt, routing,
hub-and-spoke batches, outsourcing, result entry, verification and approval exist;
nothing can be reported or delivered yet (P7). The module stays off for every clinic until P11 is VERIFIED and the
owner switches it on per clinic, then per branch delivery owner (ROLLOUT §2).
