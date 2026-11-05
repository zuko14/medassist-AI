# Kriya Diagnostic OS — Execution Status (source of truth)

> **Every agent starts here.** Then read, in order: [AGENT-PROMPT.md](AGENT-PROMPT.md) (how to work) ·
> [ARCHITECTURE.md](ARCHITECTURE.md) · [ADR.md](ADR.md) · [DATA-MODEL.md](DATA-MODEL.md) · [WORKFLOWS.md](WORKFLOWS.md) ·
> [REPORT-TEMPLATES.md](REPORT-TEMPLATES.md) · [API.md](API.md) · [UI.md](UI.md) · [SECURITY.md](SECURITY.md) ·
> [TEST-PLAN.md](TEST-PLAN.md) · [ROLLOUT.md](ROLLOUT.md) · [KRIYA-KETU.md](KRIYA-KETU.md) ·
> [GAP-ANALYSIS.md](GAP-ANALYSIS.md) · [COMPETITIVE-ANALYSIS.md](COMPETITIVE-ANALYSIS.md).
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

## 1. Current pointer (update at every pause — this is what the next agent resumes from)
| Field | Value |
|:-|:-|
| Active phase | **P0 — Discovery & baseline** |
| Next task | **DX-00** |
| Working branch | (record the branch you use; one branch per phase or lane, from `main`) |
| Last commit touching DX | `docs(diagnostic-os): …` (this document set) |
| Last VERIFIED gate | none |
| Open blockers | none |
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
| Repo / branch / HEAD | docs authored at `main` @ `56f719f` (2026-11-05) — implementer records the HEAD they start from |
| Latest migration on disk | `124_insurance_tpa_and_cashless.sql` (at authoring) |
| Migrations 115–124 applied to Supabase? | **Not at authoring** (Hospital OS open items G1-1, G5-1, G8-1, G13-1, G6-1). Record current state. DX staging needs 115 and 118 applied first |
| Python / runner | record (`.venv/Scripts/python.exe` on the owner's Windows machine; Python 3.12 + pytest in Linux sessions). The authoring session had **no pytest installed**, so no baseline was run there |
| **Baseline full suite** | record `python -m pytest -q -rs` → `<passed>, <skipped>, <failed>` — last recorded in Hospital OS: 7672 passed / 4 skipped / 0 failed (2026-10-30); later sessions 52–57 added tests |
| Pre-existing failures | record; any failure here is fixed or explicitly accepted by the owner before P1 |
| Facts F1–F10 (GAP-ANALYSIS §3) still true at HEAD | re-check with the commands in DX-01 and record |
| Owner decisions O-DX-1…11 | record answers or "default accepted" |

## 4. Phase board
| Phase | Scope | Tasks | Depends on | Can run in parallel with | Migration | Gate to close | State |
|:-|:-|:-|:-|:-|:-|:-|:-|
| P0 | Discovery & baseline | DX-00–01 | — | — | — | §3 filled | NOT_STARTED |
| P1 | Foundation: entitlement, permissions, scope, sites, owner toggle, setup page | DX-02–11 | P0 | Lane PURE (DX-13, DX-27a, DX-43a) | DX-M1 | DG1–DG7 | NOT_STARTED |
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
| DX-M1 | — | — | — | — |
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
| DX-00 | Read AGENTS.md, this folder, `docs/hospital-os/status.md`, `docs/sessions/SESSION_44_G2_LIS.md`, `SESSION_41`, `SESSION_28`; record §3 (HEAD, latest migration, Supabase state of 115–124, runner, **baseline full suite**, pre-existing failures, owner decisions) | — | §3 complete | full suite | NOT_STARTED |
| DX-01 | Re-verify F1–F10 at HEAD: `grep -n "def opd_enabled" -A15 app/services/tenant.py`; `grep -n "_lis_scope" -A12 app/routers/lis.py`; `grep -n "opd_orders_source_shape\|doctor_id" migrations/115_*.sql`; `grep -n "spawn_background_task(deliver_report" app/services/lis.py`; `grep -rn "opd_orders" app --include=*.py \| grep -v "^app/services/orders.py\|^app/services/lis.py"` (list every reader for DX-19); record deltas in §7 | DX-00 | F1–F10 confirmed or design updated | — | NOT_STARTED |

### P1 — Foundation (DX-M1)
| ID | Task | Depends | Acceptance | Tests | State |
|:-|:-|:-|:-|:-|:-|
| DX-02 | Migration DX-M1 + rollback + verify block (`clinics.dx_state`, `dx_settings`, `dx_sites`, `dx_purging`, `dx_purge_clinic` v1); add to `verify_supabase_schema.sql` | DX-01 | applies on latest; idempotent; rollback + re-apply | `test_migration_dx_m1.py` | NOT_STARTED |
| DX-03 | `app/services/tenant.py`: `OPT_IN_FEATURES["dx_enabled"]`, `dx_eligible`, `dx_access` (ADR-DX-02) | DX-02 | off by default; wildcard alone never enables; partner refused; no OPD needed | `test_dx_feature_gate.py` | NOT_STARTED |
| DX-04 | `app/services/permissions.py`: DX permissions, new roles, presets, role-implied grants (SECURITY §3), `holds_dx_permission`; staff grant cap respected | — | existing logins gain nothing except listed implied grants | `test_dx_feature_gate.py`, existing permission tests | NOT_STARTED |
| DX-05 | `app/routers/dx.py` + `app/services/dx.py`: `_dx_scope`, site-scope helper `_visit_for` skeleton, `provision_defaults` (sites from branches, departments, containers, rejection reasons later), `deactivation_preview`, routes `/setup`, `/settings` (CAS), `/sites`, `/go-live`; include router in `app/main.py` | DX-03, DX-04 | every route checks permission → tenant → entitlement → wind-down | `test_dx_feature_gate.py` | NOT_STARTED |
| DX-06 | `app/routers/platform.py`: `dx_enabled` branch (eligibility 400, provision, 409 preview, confirm → WIND_DOWN, re-enable), `GET …/dx/deactivation-preview`, purge order (`dx_purge_clinic` first); `admin/platform.html` DX card `toggleDx` | DX-05 | ADR-DX-14 table holds | `test_dx_feature_gate.py` | NOT_STARTED |
| DX-07 | `app/routers/admin.py`: `/admin/me` → `dx_access`, `dx_state`, `dx_site_ids`; `verify_credentials` confinement for `LOGISTICS_RUNNER` nav | DX-04 | | `test_dx_feature_gate.py` | NOT_STARTED |
| DX-08 | Widen `_lis_scope` (`app/routers/lis.py`) and the orders scope (`app/routers/orders.py`) with `or dx_access(clinic)` | DX-03 | OPD/IPD clinics unchanged; DX clinic reaches LIS; clinic with none refused | `test_dx_feature_gate.py`; DG5 unedited | NOT_STARTED |
| DX-09 | `app/tenancy.py` registration of DX-M1 tables; matrices assert `/admin/dx` route count | DX-05 | DG1, DG2 green | DG1, DG2 | NOT_STARTED |
| DX-10 | `tests/test_dx_no_llm.py` (AST guard, ADR-DX-10) | DX-05 | | itself | NOT_STARTED |
| DX-11 | `admin/index.html`: `data-dx` gating, `applyDxGates()`, sidebar group, **DX Setup** page (checklist, sites, settings) | DX-05–07 | live UI check 1440/390 px | manual (record §9) | NOT_STARTED |

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
| DX-19 | Filter every non-DX reader of `opd_orders` found in DX-01 (at least `orders.get_diagnostic_worklist` → `source IN ('opd','ipd')`) | DX-18 | G1 worklist identical for OPD/IPD | `test_dx_visits_e2e.py::test_g1_worklist_excludes_dx_rows`, DG5 | NOT_STARTED |
| DX-20 | `app/services/dx.py`: price quote, register (pricing in SQL), add/cancel items, visit read model with field filtering, timeline | DX-18 | W1/W2 steps 1–3 | `test_dx_visits_e2e.py` | NOT_STARTED |
| DX-21 | Routes: patient search/register (registry service under `_dx_scope`), referrers, B2B identity CRUD, visits CRUD (API P3) | DX-20 | | `test_dx_visits_e2e.py` | NOT_STARTED |
| DX-22 | Billing glue: bill/issue/receipts/refunds/payment link via `opd_billing`; prepaid apply for bookings; bill PDF A5 + 80 mm | DX-20 | bill = Σ items; prepaid once | `test_dx_visits_e2e.py` | NOT_STARTED |
| DX-23 | Today's bookings list + convert (`source_appointment_id`, unique per booking) | DX-22 | W3 | `test_dx_visits_e2e.py` | NOT_STARTED |
| DX-24 | `data_retention.py` DX section | DX-18 | | `test_dx_data_retention.py` | NOT_STARTED |
| DX-25 | UI **Registration** page (incl. Today's bookings) | DX-21–23 | live UI, keyboard flow | manual | NOT_STARTED |

**P3 DoD:** W1 steps 1–3, W2, W3, W12 run end-to-end on the sandbox; concurrency cases 1–2 (TEST-PLAN §3) pass.

### P4 — Samples and collection (DX-M4)
| ID | Task | Depends | Acceptance | Tests | State |
|:-|:-|:-|:-|:-|:-|
| DX-26 | Migration DX-M4 (rejection reasons, sample guard, collect / bind / reject / receive-same-site / store functions) | P3 | ADR-DX-05 | `test_dx_samples.py` | NOT_STARTED |
| DX-27 | `app/services/dx_barcode.py` Code128 B/C (Lane PURE = DX-27a) + label PDF + ZPL in `dx_pdf.py` | — | published vectors | `test_dx_labels.py` | NOT_STARTED |
| DX-28 | Routes: collection queue, labels, bind, collect, reject (+ recollection WhatsApp: in-session or `dx_recollection_required` template), receive same-site, track sample | DX-26, DX-27 | W1 steps 4–6, W7 | `test_dx_samples.py` | NOT_STARTED |
| DX-29 | Home collection: `prepare-route`, phlebotomist samples route + allowlist, portal Samples panel | DX-28 | W4; phlebotomist still confined | `test_dx_samples.py`, `test_home_collection.py` unedited | NOT_STARTED |
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
| DX-37 | LIS: skip `deliver_report` for `source='dx'`; LIS entry/validate routes return `409 dx_use_dx_route` for dx orders | DX-36 | F4 closed; OPD/IPD unchanged | `test_dx_results_e2e.py`, DG5 | NOT_STARTED |
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
| DX-45 | Releases: auto policy after approval, render + upload in background (`spawn_background_task`), `upload_and_send(dx-<id>)`, dues hold + payment-link settlement re-queue, routing (patient / referrer / account), resend, print log, delivery owner + shadow + connector guard | DX-44 | W10, W11, ADR-DX-15 | `test_dx_reports_e2e.py` | NOT_STARTED |
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
| — | — | — | — | — |

## 8. Known issues and limitations
- At authoring, migrations 115–124 (which DX builds on) are not applied to Supabase; DX cannot reach staging first.
- `admin/index.html` is a very large single file — UI lanes must use the DX markers (UI.md §1) to avoid conflicts.
- Approval requires doctor-linked logins with registration numbers; centres where non-doctor scientists sign
  reports under state rules need an owner decision (signatory eligibility is data in `dx_signatories`, but the 118
  CHECK requires a registration number).

## 9. Verification evidence (paste command and summary line; newest first)
| Date (IST) | Task / gate | Command | Result |
|:-|:-|:-|:-|
| — | — | — | — |

## 10. Log
| Date (IST) | Entry |
|:-|:-|
| 2026-11-05 | Document set authored (no code): competitive research (MocDoc screenshots + public pages for MocDoc/CrelioHealth; direct fetch blocked by network policy), gap analysis against `main` @ `56f719f`, architecture, 18 ADRs, data model DX-M1…M9, workflows W1–W18, report/template/label spec, API, UI, security, test plan, rollout, assistants, agent prompts. Baseline suite not run in the authoring session (no pytest installed). |

## 11. Next exact action
Start **DX-00** with the master prompt in [AGENT-PROMPT.md](AGENT-PROMPT.md) §1.

## 12. Release decision
**NOT STARTED.** Nothing is implemented. The module stays off for every clinic until P11 is VERIFIED and the
owner switches it on per clinic, then per branch delivery owner (ROLLOUT §2).
