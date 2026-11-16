# Kriya Pharmacy OS — Execution Status (source of truth)

> **Every agent starts here.** Then read, in order: [AGENT-PROMPT.md](AGENT-PROMPT.md) (how to work) ·
> **[AUDIT.md](AUDIT.md)** (code evidence — read before touching any existing file) · **[VERIFICATION.md](VERIFICATION.md)**
> (adversarial review: defects D1–D15, latent bugs L1–L2, loopholes X1–X19, zero-impact matrix — binding) ·
> [ARCHITECTURE.md](ARCHITECTURE.md) ·
> [ADR.md](ADR.md) · [DATA-MODEL.md](DATA-MODEL.md) · [WORKFLOWS.md](WORKFLOWS.md) · [API.md](API.md) · [UI.md](UI.md) ·
> [SECURITY.md](SECURITY.md) · [TEST-PLAN.md](TEST-PLAN.md) · [ROLLOUT.md](ROLLOUT.md) · [KRIYA-KETU.md](KRIYA-KETU.md) ·
> [GAP-ANALYSIS.md](GAP-ANALYSIS.md) · [COMPETITIVE-ANALYSIS.md](COMPETITIVE-ANALYSIS.md) ·
> [ADVANCED-FEATURES.md](ADVANCED-FEATURES.md) (staff accelerators A1–A14).
>
> States: `NOT_STARTED` · `IN_PROGRESS` · `BLOCKED` (say why) · `IMPLEMENTED` (code written, evidence incomplete) ·
> `VERIFIED` (the listed command was run and passed — summary pasted in §9) · `DEFERRED` (reason given).
> **Rules:** never mark VERIFIED without pasting the command's summary line; update this file in the same commit as
> the work; one task IN_PROGRESS per agent; never start a task whose dependencies are not VERIFIED.

## 0. Mission and scope
Build **Kriya Pharmacy OS** (lane **RX**): an optional, owner-enabled, tenant-isolated module that runs a hospital's
pharmacy — item master → purchase & GRN → append-only stock ledger with FEFO and a hard expiry lock → multi-store
transfers → OPD POS that fulfils the doctor's signed prescription → IPD ward indents dispensed and billed atomically
→ returns and credit notes → statutory Schedule H/H1/X/narcotic registers → stock audits → WhatsApp chronic refills and
Kriya Ketu stock lookups. **Nothing changes for any clinic that has not enabled `pharmacy_enabled`** (ARCHITECTURE §8),
and AI never prescribes, doses or substitutes (ADR-RX-06).

## 1. Current pointer (update at every pause — this is what the next agent resumes from)
| Field | Value |
|:-|:-|
| Active phase | **Specification complete and verified** (review round 1, VERIFICATION.md, 2026-11-16). Implementation not started |
| Next task | **RX-00** (P0 baseline) — after the owner reviews the open decisions in ADR.md (O-1…O-6; defaults are in force, so P0–P6 are not blocked) |
| Working branch | spec on `rx/spec` (from `main` @ `e5f99b5`, not pushed); create `rx/p0-baseline` from it when RX-00 starts |
| Last commit touching RX | `docs(rx): forensic verification round 1 and advanced staff features` on `rx/spec` |
| Last VERIFIED gate | none (no code) |
| Open blockers | none for P0–P6. P7 go-live needs O-4 (legal confirmation of register formats). P8 refill sends need Meta-approved templates in each clinic's WABA |
| Lanes running in parallel | none |

## 2. Resume protocol (after a pause of any length)
1. `git fetch origin && git status && git log --oneline -15` — see what landed since §1 was written.
2. Read §1, §4, §6 and the §10 log tail. Read AGENT-PROMPT.md §1–§3.
3. Re-run the **last VERIFIED gate** (§4 names it). Red → the cause is new code on `main`; fix or report before
   continuing — never build on a red baseline.
4. Check migrations added to `main` since §5's last row; if one took "your" number (134–142), renumber your pending
   files and every doc reference in the same commit.
5. Re-locate every AUDIT.md line you depend on by symbol; record moved lines in §7.
6. Continue with §1 "Next task". If a task is IN_PROGRESS by an agent that stopped, inspect its branch, finish or
   restart it, and note the takeover in §10.

## 3. Discovery and baseline (P0) — fill before any code
| Item | Result |
|:-|:-|
| Repo / branch / HEAD | spec written at `main` @ `e5f99b5` |
| Latest migration on disk | `133_dx_instruments_qc.sql` → RX-M1 takes **134** |
| `TENANT_OWNED_TABLES` size | 135 (41 dx, 19 ipd, 10 opd) at `e5f99b5` |
| Migrations 125–133 applied to Supabase? | not verified (DX status says owner action pending) — RX migrations must be applied **after** them |
| Full-suite baseline count | fill in RX-00 (`python -m pytest -q -rs`, summary line) |
| Lint baseline | fill in RX-00 (`TOTAL_BARE_BASELINE` must be 0) |

## 4. Phase board
| Phase | Name | Migration | Gate to close | State |
|:-|:-|:-|:-|:-|
| P0 | Baseline & audit re-verification | — | RG1–RG7 green on `main`, counts in §3 | NOT_STARTED |
| P1 | Foundation: gate, roles, stores, setup | 134 (RX-M1) | RG1–RG7 + P1 DoD | NOT_STARTED |
| P2 | Catalogue, batches, stock ledger | 135, 136 | RG1–RG7 + ledger invariants | NOT_STARTED |
| P3 | Purchase: suppliers, PO, GRN, supplier returns | 137 | RG1–RG7 | NOT_STARTED |
| P4 | OPD POS, tax, returns, shifts | 138 | RG1–RG8 + POS timing | NOT_STARTED |
| P5 | IPD indents, dispense, MAR link, IPD returns | 139 | RG1–RG8, RG5 unedited | NOT_STARTED |
| P6 | Transfers & stock audits | 140 | RG1–RG8 | NOT_STARTED |
| P7 | Statutory registers | 141 | RG1–RG8 + chain verify | NOT_STARTED |
| P8 | Refills & alerts | 142 | RG1–RG8 | NOT_STARTED |
| P9 | Kriya Ketu, dashboard | — | RG1–RG7 + registry tests | NOT_STARTED |
| P10 | Reports: GSTR-1, registers of purchase/sale, exports | — | RG1–RG7 + golden files | NOT_STARTED |
| P11 | Hardening, latency, release readiness, cutover rehearsal | — | all gates + release readiness | NOT_STARTED |

Gates RG1–RG8: TEST-PLAN.md §1. Every phase's Definition of Done includes: live UI check 1440/390 px (dark + light)
for pages added; no existing test edited; this file updated.

## 5. Migration ledger
| No. | File | Task | Content | Rollback | State |
|:-|:-|:-|:-|:-|:-|
| 134 | `134_rx_foundation.sql` | RX-02 | `clinics.rx_state/rx_settings`, helpers, `rx_stores`, `rx_purge_clinic` v1 | `rollback/134_rollback_rx_foundation.sql` | NOT_STARTED |
| 135 | `135_rx_catalogue.sql` | RX-12 | manufacturers, generics, items, barcodes, aliases, item changes | `rollback/135_…` | NOT_STARTED |
| 136 | `136_rx_stock_ledger.sql` | RX-17 | batches, movements, balances, triggers, views | `rollback/136_…` | NOT_STARTED |
| 137 | `137_rx_purchase.sql` | RX-22 | suppliers, PO, GRN, supplier returns | `rollback/137_…` | NOT_STARTED |
| 138 | `138_rx_pos.sql` | RX-28 | sales, items, payments, fulfilments, returns, credit notes, overrides; **redefines `opd_close_shift`** (+ pharmacy cash, D1) | `rollback/138_…` (restores the 116 `opd_close_shift` body) | NOT_STARTED |
| 139 | `139_rx_ipd.sql` | RX-37 | indents, dispenses, `ipd_charges.source` widen (NOT VALID + VALIDATE), `rx_mar_links`, `rx_empty_returns` — no other IPD table touched, no trigger on IPD tables | `rollback/139_…` (restores CHECK only if no `source='pharmacy'` rows) | NOT_STARTED |
| 140 | `140_rx_transfers_audits.sql` | RX-41 | transfers, audits, audit delta trigger | `rollback/140_…` | NOT_STARTED |
| 141 | `141_rx_registers.sql` | RX-48 | register entries, append/verify functions, redefined writers | `rollback/141_…` | NOT_STARTED |
| 142 | `142_rx_refills_alerts.sql` | RX-55 | refill plans/requests, alerts | `rollback/142_…` | NOT_STARTED |

## 6. Task matrix
Columns: ID · task · depends · acceptance · tests · state. Every task's "tests" must exist and pass before VERIFIED.

### P0 — Baseline
| ID | Task | Depends | Acceptance | Tests | State |
|:-|:-|:-|:-|:-|:-|
| RX-00 | Branch `rx/p0-baseline`; run RG1–RG7 on `main`; record counts in §3 | — | all green; counts pasted | RG1–RG7 | NOT_STARTED |
| RX-01 | Re-verify every AUDIT.md row by symbol; record moved lines in §7; update design if a finding changed | RX-00 | A1–A48 confirmed or docs updated in same commit | — (evidence in §7) | NOT_STARTED |

### P1 — Foundation (RX-M1, 134)
| ID | Task | Depends | Acceptance | Tests | State |
|:-|:-|:-|:-|:-|:-|
| RX-02 | Migration 134 + rollback + verify block | RX-01 | applies on 133; idempotent; rollback + re-apply | `test_migration_rx_m1.py` | NOT_STARTED |
| RX-03 | `tenant.py`: `OPT_IN_FEATURES["pharmacy_enabled"]`, `rx_eligible`, `rx_access` (ADR-RX-09) | RX-02 | off by default; needs OPD; wildcard never enables | `test_rx_feature_gate.py` | NOT_STARTED |
| RX-04 | `permissions.py`: RX perms, PHARMACIST/PHARMACY_MANAGER/STORE_KEEPER, implied grants, `holds_rx_permission`, `allowed_store_ids` | — | existing logins gain only SECURITY §3.2 grants, only while rx_access | `test_rx_feature_gate.py`, existing permission tests | NOT_STARTED |
| RX-05 | `app/routers/rx.py` + `app/services/rx.py`: `_rx_scope`, settings (CAS), stores CRUD, setup checklist, deactivation preview; mount in `app/main.py` | RX-03, RX-04 | check order permission → tenant → access → wind-down → store | `test_rx_feature_gate.py` | NOT_STARTED |
| RX-06 | `platform.py`: `pharmacy_enabled` toggle (eligibility 400, provision default store, 409 preview, confirm → WIND_DOWN, re-enable; `None` = disable); refuse `opd_enabled` off while `rx_state ≠ OFF` (D7); owner console card | RX-05 | ADR-RX-09 lifecycle | `test_rx_feature_gate.py` | NOT_STARTED |
| RX-07 | `/admin/me` + `rx_access`, `rx_state`, `rx_store_ids`, `rx_perms` | RX-04 | additive keys | `test_rx_feature_gate.py` | NOT_STARTED |
| RX-08 | Platform purge: gate also counts `rx_sales`/`rx_stock_movements`; `rx_purge_clinic` first when the `rx_state` column exists (D8, A60) | RX-02 | purge of RX clinic leaves zero rx rows and succeeds; non-RX purge unchanged | `test_migration_rx_m1.py::test_platform_purge_order` | NOT_STARTED |
| RX-09 | `tenancy.py` registration (134 tables); `test_rx_tenancy_registry.py`; `test_rx_route_matrix.py` skeleton | RX-05 | RG1, RG2 green | RG1, RG2 | NOT_STARTED |
| RX-10 | `tests/test_rx_no_llm.py` (ADR-RX-06) | RX-05 | | itself | NOT_STARTED |
| RX-11 | UI: Pharmacy nav group, `applyRxGates()`, **Pharmacy Setup** page (`<!-- RX:foundation -->`) | RX-05–07 | hidden for non-RX clinic; 1440/390 | manual (§9) | NOT_STARTED |
**P1 DoD:** no clinic's behaviour changed; owner can enable / wind down / re-enable on a sandbox clinic; RG1–RG7 green.

### P2 — Catalogue & ledger (135, 136)
| ID | Task | Depends | Acceptance | Tests | State |
|:-|:-|:-|:-|:-|:-|
| RX-12 | Migration 135 (catalogue) | P1 | immutables, schedule downgrade refused, change log | `test_migration_rx_m2.py` | NOT_STARTED |
| RX-13 | `rx_catalogue.py` + item/generic/manufacturer/barcode routes | RX-12 | API §2 | `test_rx_catalogue_api.py` | NOT_STARTED |
| RX-14 | Item CSV/XLSX import with dry run (ROLLOUT §4.2) | RX-13 | idempotent; row errors | `test_rx_catalogue_api.py::test_import_*` | NOT_STARTED |
| RX-15 | Deterministic resolver (`/items/resolve`) and substitutes (same generic) | RX-13 | stable ordering; no LLM | `test_rx_catalogue_api.py::test_resolve_*` | NOT_STARTED |
| RX-16 | UI **Drug Master** | RX-13–15 | UI §7 | manual | NOT_STARTED |
| RX-17 | Migration 136 (batches, movements, balances, triggers, views) | RX-12 | ADR-RX-01/02 invariants | `test_migration_rx_m3.py` | NOT_STARTED |
| RX-18 | `rx_stock.py`: FEFO, ledger, stock, expiry, reorder reads | RX-17 | API §3 reads | `test_rx_stock_api.py` | NOT_STARTED |
| RX-19 | Write-offs, batch block/unblock | RX-17 | reasons; expired write-off allowed | `test_rx_stock_api.py` | NOT_STARTED |
| RX-20 | Opening stock import (ROLLOUT §4.3) | RX-17, RX-14 | only before first non-opening movement; expired rejected | `test_rx_stock_api.py::test_opening_*` | NOT_STARTED |
| RX-21 | UI **Stock & Expiry** | RX-18–20 | UI §5 | manual | NOT_STARTED |

### P3 — Purchase (137)
| ID | Task | Depends | Acceptance | Tests | State |
|:-|:-|:-|:-|:-|:-|
| RX-22 | Migration 137 | P2 | | `test_migration_rx_m4.py` | NOT_STARTED |
| RX-23 | Suppliers (GSTIN checksum) | RX-22 | | `test_rx_purchase.py` | NOT_STARTED |
| RX-24 | Purchase orders (draft/send/cancel/PDF) | RX-23 | `PO-` gapless | `test_rx_purchase.py` | NOT_STARTED |
| RX-25 | GRN draft/calc/post/void/labels (W1) | RX-24 | TEST-PLAN §4.3 vectors | `test_rx_purchase.py` | NOT_STARTED |
| RX-26 | Supplier returns + debit note (W6) | RX-25 | expired batches can leave | `test_rx_purchase.py` | NOT_STARTED |
| RX-27 | UI **Purchase & GRN** | RX-23–26 | UI §4 | manual | NOT_STARTED |

### P4 — POS (138)
| ID | Task | Depends | Acceptance | Tests | State |
|:-|:-|:-|:-|:-|:-|
| RX-28 | Migration 138 | P3 | | `test_migration_rx_m5.py` | NOT_STARTED |
| RX-29 | POS lookup (token/MRN/phone/QR) + Rx line mapping + alias learning | RX-28, RX-15 | W2 steps 1–3 | `test_rx_pos.py` | NOT_STARTED |
| RX-30 | `rx_tax.py` + SQL `rx_gst_split_inclusive`, `rx_unit_price` | RX-28 | TEST-PLAN §4.1/4.2; property test | `test_rx_tax.py` | NOT_STARTED |
| RX-31 | Draft lines: server pricing, FEFO pick/split, warnings, discount cap | RX-29, RX-30 | | `test_rx_pos.py` | NOT_STARTED |
| RX-32 | `rx_complete_sale` + route (schedule checks, payments, fulfilments, idempotency) | RX-31 | W2/W3 lists | `test_rx_pos.py`, `test_rx_concurrency.py` | NOT_STARTED |
| RX-33 | Receipts 80 mm / A5 PDF + print view; WhatsApp e-bill (template) | RX-32 | UI §2.5 fields | `test_rx_pos.py::test_receipt_*` | NOT_STARTED |
| RX-34 | Shift integration: 138 redefines SQL `opd_close_shift` adding `rx_sale_payments` cash (D1); `get_collections_summary` gains additive `pharmacy_*` keys | RX-32 | expected cash identical for shifts without pharmacy cash; pharmacy cash reconciles | `test_rx_pos.py::test_shift_*`, `test_rx_zero_impact.py::test_close_shift_unchanged_without_rx`; RG5 | NOT_STARTED |
| RX-35 | Returns, credit notes, same-day void (W5a) | RX-32 | | `test_rx_returns.py` | NOT_STARTED |
| RX-36 | UI **Pharmacy POS** (keyboard map, drawer, mapping, receipt) | RX-29–35 | ≤ 3 s timing script | manual + timing (§9) | NOT_STARTED |

### P5 — IPD (139)
| ID | Task | Depends | Acceptance | Tests | State |
|:-|:-|:-|:-|:-|:-|
| RX-37 | Migration 139 (NOT VALID + VALIDATE for CHECK/FK) | P4 | 111/115/116/117 suites unedited green | `test_migration_rx_m6.py`, RG5 | NOT_STARTED |
| RX-38 | Indents: indentable list, raise, cancel, queue; order-end cascade | RX-37 | W4 steps 1–3 | `test_rx_ipd.py` | NOT_STARTED |
| RX-39 | `rx_dispense_indent` (admission lock first, order re-check, D9/D10), ward issue, receive, slip PDF; MAR route pops `rx_dispense_line_id` and writes `rx_mar_links` after the unchanged insert (D2) | RX-38 | ADR-RX-04 list | `test_rx_ipd.py`, `test_rx_concurrency.py` | NOT_STARTED |
| RX-40 | IPD returns (W5b) + UI **Ward Indents** + MAR batch select | RX-39 | | `test_rx_ipd.py`, manual | NOT_STARTED |

### P6 — Transfers & audits (140)
| ID | Task | Depends | Acceptance | Tests | State |
|:-|:-|:-|:-|:-|:-|
| RX-41 | Migration 140 | P5 | | `test_migration_rx_m7.py` | NOT_STARTED |
| RX-42 | Transfers dispatch/receive/short write-off; controlled two-person | RX-41 | W7 | `test_rx_transfers.py` | NOT_STARTED |
| RX-43 | Store scope for ward sub-stores in all RX routes | RX-42 | nurse ward A cannot touch ward B (404) | `test_rx_route_matrix.py` | NOT_STARTED |
| RX-44 | UI **Transfers** (desktop + 390 px receive) | RX-42 | | manual | NOT_STARTED |
| RX-45 | Audits start/count/review | RX-41 | | `test_rx_audit.py` | NOT_STARTED |
| RX-46 | Audit delta trigger, freeze, post with second approver | RX-45 | W8 | `test_rx_audit.py`, `test_rx_concurrency.py` | NOT_STARTED |
| RX-47 | UI **Stock Audit** (blind count, mobile count) | RX-46 | | manual | NOT_STARTED |

### P7 — Registers (141)
| ID | Task | Depends | Acceptance | Tests | State |
|:-|:-|:-|:-|:-|:-|
| RX-48 | Migration 141 (register table, `rx_register_append`, `rx_register_verify`) | P6 | | `test_migration_rx_m8.py` | NOT_STARTED |
| RX-49 | Wire register writes into GRN, sale, void, return, dispense, ward issue, transfer, audit, write-off functions (redefinitions in 141) | RX-48 | every controlled movement has exactly one register row | `test_rx_registers.py` | NOT_STARTED |
| RX-50 | Register list/export PDF+CSV/verify routes; nightly verify + ledger check jobs | RX-49 | W9 | `test_rx_registers.py` | NOT_STARTED |
| RX-51 | UI **Drug Registers** | RX-50 | UI §8 | manual | NOT_STARTED |

### P8 — Refills & alerts (142)
| ID | Task | Depends | Acceptance | Tests | State |
|:-|:-|:-|:-|:-|:-|
| RX-55 | Migration 142 | P7 | | `test_migration_rx_m9.py` | NOT_STARTED |
| RX-56 | `rx_alerts_scan` job + alert routes + STAT indent notifications | RX-55 | dedupe/day; OFF clinics skipped | `test_rx_alerts.py` | NOT_STARTED |
| RX-57 | Refill plan creation on sale (closed frequency map only; exclusions) | RX-55 | | `test_rx_refills.py` | NOT_STARTED |
| RX-58 | `rx_refill_reminders` job (`distributed_job_lock`), opt-in + `accepts_engagement` gate, button templates, `rx_refill:`/`rx_refill_off:` prefix hook beside the dental-review hook (D3) | RX-57 | W10; typed text never intercepted; webhook SLA untouched (async worker) | `test_rx_refills.py`, RG4 | NOT_STARTED |
| RX-59 | UI **Refill Requests** + verify/pack/reject | RX-58 | | manual | NOT_STARTED |

### P9 — Ketu & dashboard
| ID | Task | Depends | Acceptance | Tests | State |
|:-|:-|:-|:-|:-|:-|
| RX-60 | Ketu registry actions (KRIYA-KETU §2), `_rx_on`, `PAGES` entries | P8 | RT-14 | `test_assistant_registry.py`, `test_rx_ketu.py` | NOT_STARTED |
| RX-61 | `/admin/rx/stock/answer` + `present.rx_stock`; voice `PHARMACY_REFILL` | RX-60 | KRIYA-KETU §2.1, §3 | `test_rx_ketu.py` | NOT_STARTED |
| RX-62 | Dashboard route + UI **Pharmacy Dashboard** | P8 | UI §9 | `test_rx_stock_api.py::test_dashboard_*`, manual | NOT_STARTED |

### P10 — Reports
| ID | Task | Depends | Acceptance | Tests | State |
|:-|:-|:-|:-|:-|:-|
| RX-52 | GSTR-1: pharmacy B2C by rate + HSN summary + credit notes; non-RX golden file unchanged | P4 | | `test_rx_reports.py` | NOT_STARTED |
| RX-53 | CSV reports (sales/purchase registers, stock valuation, movement, expiry, margin, day-book) with formula-injection guard | P6 | | `test_rx_reports.py` | NOT_STARTED |
| RX-54 | Distributor invoice CSV/XLSX → GRN draft (stretch; may be DEFERRED by owner) | P3 | | `test_rx_purchase.py::test_invoice_import_*` | NOT_STARTED |

### P11 — Hardening & release
| ID | Task | Depends | Acceptance | Tests | State |
|:-|:-|:-|:-|:-|:-|
| RX-63 | Concurrency suite complete (TEST-PLAN §3) + latency check (≤ 5% regression) | P10 | | RG8, §9 latency table | NOT_STARTED |
| RX-64 | Cutover rehearsal on sandbox with a real legacy export (ROLLOUT §5) | P10 | reconciliation ≤ 0.5% | manual, §9 | NOT_STARTED |
| RX-65 | `tests/test_rx_release_readiness.py` (lifecycle, rollback chain 142→133, purge) | RX-63 | | itself | NOT_STARTED |
| RX-66 | AGENTS.md row + `docs/agent-context` updates; ROLLOUT owner checklist handed over | RX-65 | | — | NOT_STARTED |

### Verification fixes and advanced features (VERIFICATION.md, ADVANCED-FEATURES.md)
| ID | Task | Phase | Depends | Acceptance | Tests | State |
|:-|:-|:-|:-|:-|:-|:-|
| RX-67 | `delete_branch`: on FK violation (23503) fall back to the existing deactivate path (latent bug L1) | P1 | RX-02 | branch with IPD ward / DX site / RX store → deactivated, not 500; clean branch still deleted | `test_admin_branches.py::test_delete_branch_with_referencing_records_deactivates` | VERIFIED |
| RX-68 | DPDP erasure: per-member 23503 → redact and continue (latent bug L2); delete the patient's refill plans/requests | P1 (plans part in P8) | RX-02 | erasure completes for members referenced by IPD/DX/RX; registers retained | `test_opd_data_retention.py::test_family_member_with_non_opd_fk_references_is_redacted` | VERIFIED |
| RX-69 | Rx Inbox (A1) | P4 | RX-32 | | `test_rx_pos.py::test_inbox_*` | NOT_STARTED |
| RX-70 | Park / recall bills (A2) | P4 | RX-31 | | `test_rx_pos.py::test_park_*` | NOT_STARTED |
| RX-71 | GS1 / pharma QR parser at GRN and POS (A3) | P3 | RX-25 | parser vectors | `test_rx_gs1.py` | NOT_STARTED |
| RX-72 | Dispensing labels (A4) | P4 | RX-33 | verbatim prescriber text; closed vocabulary only | `test_rx_labels.py` | NOT_STARTED |
| RX-73 | Repeat last bill (A5) | P4 | RX-32 | scheduled items need valid Rx | `test_rx_pos.py::test_repeat_*` | NOT_STARTED |
| RX-74 | Batch recall trace + confirmed notify (A6) | P7 | RX-50 | | `test_rx_recall.py` | NOT_STARTED |
| RX-75 | ADU reorder suggestions, ABC/VED/FSN, dead stock (A7) | P10 | P8 | suggest-then-apply only | `test_rx_metrics.py` | NOT_STARTED |
| RX-76 | Expiry-return planner (A8) | P8 | RX-26 | drafts only | `test_rx_purchase.py::test_planner_*` | NOT_STARTED |
| RX-77 | Manager override tokens (A9) | P4 | RX-32 | single-use, 60 s, approver ≠ requester | `test_rx_pos.py::test_override_*` | NOT_STARTED |
| RX-78 | Ward par levels + rack-sorted pick lists (A10) | P6 | RX-42 | drafts only | `test_rx_transfers.py::test_par_*` | NOT_STARTED |
| RX-79 | Narcotic empty-ampoule control (A11) | P7 | RX-49 | two-person; override with reason | `test_rx_registers.py::test_empties_*` | NOT_STARTED |
| RX-80 | Lost-sale / pending-balance call list (A12) | P9 | P8 | no automatic messages | `test_rx_reports.py::test_lost_sale_list` | NOT_STARTED |
| RX-81 | Pharmacy shift Z-report (A13) | P4 | RX-34 | matches `opd_close_shift` expected cash | `test_rx_pos.py::test_z_report_*` | NOT_STARTED |
| RX-82 | Medicine history + recorded allergies panel, display only (A14) | P4 | RX-29 | no checking logic | `test_rx_pos.py::test_history_panel_*` | NOT_STARTED |
| RX-83 | `tests/test_rx_zero_impact.py`: one test per VERIFICATION §7 row | P1→P11 (grows per phase) | — | every row green | itself | NOT_STARTED |

### Deferred (owner decision needed)
| ID | Item | Reason |
|:-|:-|:-|
| RX-D2 | Credit / on-account sales | O-1 |
| RX-D3 | Offline OTC POS queue | G15 — conflicts with server-enforced guarantees |
| RX-D4 | Home delivery logistics for refills | O-6 |
| RX-D5 | Cold-chain temperature logging | G28 |
| RX-D6 | Standalone retail pharmacy without OPD | G30 |

## 7. Audit deltas (lines that moved / findings that changed)
| Date | AUDIT ID | Old | New | Design impact |
|:-|:-|:-|:-|:-|
| — | — | — | — | — |

## 8. Non-functional requirements
| ID | Requirement | Measured by |
|:-|:-|:-|
| NFR-1 | POS: scan → line < 300 ms p95; complete → receipt < 1.5 s p95; 3-line Rx ≤ 3 s end to end | P4 timing script, P11 latency |
| NFR-2 | Zero regression for non-RX clinics (≤ 5% p95 latency on webhook / OPD issue / IPD admission) | P11 |
| NFR-3 | Ledger == balance for every clinic every night | `rx_ledger_check` |
| NFR-4 | Register chain verifies every night | `rx_register_verify` |
| NFR-5 | All dates IST; all money integer paise | review + tests |

## 9. Verification log (paste command summaries here)
| Date | Task / gate | Command | Result |
|:-|:-|:-|:-|
| — | — | — | — |

## 10. Session log
| Date | Agent | What happened |
|:-|:-|:-|
| 2026-11-16 | Claude Code | Forensic audit (AUDIT.md, A1–A48, brief corrections §0) and 15-document specification suite written. No code, no migrations. |
| 2026-11-16 | Claude Code | Review round 1 (VERIFICATION.md): 15 design defects fixed (2 critical: shift cash D1, MAR guard/purge D2), 2 latent production bugs found (L1 branch delete 500, L2 erasure abort) with fix tasks RX-67/68, 19 loopholes closed, zero-impact matrix; ADVANCED-FEATURES.md A1–A14 (RX-69…RX-82). |
| 2026-11-16 | Antigravity | Latent bugs RX-67 and RX-68 implemented on main and merged into rx/spec: safe branch delete with FK deactivation fallback (test_admin_branches.py, 4 tests passed) and robust family member erasure (test_opd_data_retention.py, 5 tests passed). Both tasks marked VERIFIED. |
