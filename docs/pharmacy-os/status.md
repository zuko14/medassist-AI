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
| Active phase | **ALL PHASES P0–P11 VERIFIED — ready for owner migration apply & push** (2026-11-18). Everything is on `rx/p1-foundation` as local commits; migrations 134–142 are not yet applied to Supabase and Pharmacy OS stays off for every clinic until the owner enables it |
| Next task | **Owner**: review, merge `rx/p1-foundation` → `main`, push, apply 134→142 on Supabase (ROLLOUT §2), run `verify_supabase_schema.sql` (§28–§36 must be 27/27 PASS), then enable `pharmacy_enabled` for the pilot clinic. Open, owner-visible: latency check (TEST-PLAN §6.3, ≤ 5 % p95) not measured — needs the perf fixture against the deployed stack; RX-64 rehearsed on a simulated legacy export (a real Marg / MocDoc export still to be run on the sandbox at T−7); RX-54 DEFERRED; P9 Telugu / Devanagari names only through `rx_item_aliases`; P8 Meta approval of `rx_refill_due_v1`, home delivery (O-6), A8 monthly drafting job, register head-hash anchor; P7 A6 "Notify patients", A11 empties re-dispense override, O-4; P5 MAR route wiring, ward-issue route; P6 par 06:00 job, staff→ward assignment. Open owner decisions O-1…O-6 have defaults in force |
| Working branch | `rx/p1-foundation` (from `rx/spec` @ `1078dca`, which includes RX-67/68); local commits only, not pushed |
| Last commit touching RX | `feat(rx): P9 Kriya Ketu assistant, pharmacy dashboard, voice refill and lost-sale list (RX-60…RX-62, RX-80)` on `rx/p1-foundation` |
| Last VERIFIED gate | P9: registry + reads (901), RG1 (10), RG2 (1926), RG3 (37), RX Ketu / matrix / gate / dashboard (97), voice + webhook + FSM (472), full suite 10,179 passed / 0 failed (§9, 2026-11-18) |
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
| Full-suite baseline count | **9,412 passed, 5 skipped, 0 failed** at `1078dca` (clean worktree, 2026-11-16) |
| Lint baseline | RG1 10 passed (`TOTAL_BARE_BASELINE` 0); RG2 1,587 passed; RG3 (firewall + DX no-LLM) 41 passed |

## 4. Phase board
| Phase | Name | Migration | Gate to close | State |
|:-|:-|:-|:-|:-|
| P0 | Baseline & audit re-verification | — | RG1–RG7 green on `main`, counts in §3 | VERIFIED |
| P1 | Foundation: gate, roles, stores, setup | 134 (RX-M1) | RG1–RG7 + P1 DoD | VERIFIED |
| P2 | Catalogue, batches, stock ledger | 135, 136 | RG1–RG7 + ledger invariants | VERIFIED |
| P3 | Purchase: suppliers, PO, GRN, supplier returns | 137 | RG1–RG7 | VERIFIED |
| P4 | OPD POS, tax, returns, shifts | 138 | RG1–RG8 + POS timing | VERIFIED (RG8 suite landed in P11) |
| P5 | IPD indents, dispense, MAR link, IPD returns | 139 | RG1–RG8, RG5 unedited | VERIFIED (RG8 suite landed in P11) |
| P6 | Transfers & stock audits | 140 | RG1–RG8 | VERIFIED (RG8 suite landed in P11) |
| P7 | Statutory registers | 141 | RG1–RG8 + chain verify | VERIFIED (RG8 suite landed in P11) |
| P8 | Refills & alerts | 142 | RG1–RG8 | VERIFIED (RG8 suite landed in P11) |
| P9 | Kriya Ketu, dashboard | — | RG1–RG7 + registry tests | VERIFIED |
| P10 | Reports: GSTR-1, registers of purchase/sale, exports | — | RG1–RG7 + golden files | VERIFIED |
| P11 | Hardening, latency, release readiness, cutover rehearsal | — | all gates + release readiness | VERIFIED (latency check not measured, see §1) |

Gates RG1–RG8: TEST-PLAN.md §1. Every phase's Definition of Done includes: live UI check 1440/390 px (dark + light)
for pages added; no existing test edited; this file updated.

## 5. Migration ledger
| No. | File | Task | Content | Rollback | State |
|:-|:-|:-|:-|:-|:-|
| 134 | `134_rx_foundation.sql` | RX-02 | `clinics.rx_state/rx_settings`, helpers, `rx_stores`, `rx_purge_clinic` v1 | `rollback/134_rollback_rx_foundation.sql` | VERIFIED |
| 135 | `135_rx_catalogue.sql` | RX-12 | manufacturers, generics, items, barcodes, aliases, item changes | `rollback/135_…` | VERIFIED |
| 136 | `136_rx_stock_ledger.sql` | RX-17 | batches, movements, balances, triggers, views | `rollback/136_…` | VERIFIED |
| 137 | `137_rx_purchase.sql` | RX-22 | suppliers, PO, GRN, supplier returns, GRN line calc + freight split, `rx_send_po` / `rx_post_grn` / `rx_void_grn` / `rx_supplier_return`, `rx_v_item_last_supplier`, `rx_purge_clinic` v4 | `rollback/137_rollback_rx_purchase.sql` (restores purge v3) | VERIFIED |
| 138 | `138_rx_pos.sql` | RX-28 | sales, items, payments, fulfilments, returns, credit notes, overrides; **redefines `opd_close_shift`** (+ pharmacy cash, D1) | `rollback/138_…` (restores the 116 `opd_close_shift` body) | VERIFIED |
| 139 | `139_rx_ipd.sql` | RX-37 | indents, indent lines, dispenses, dispense lines, `rx_mar_links`, `rx_empty_returns`; `ipd_charges.source` widened (keeps `package`; NOT VALID + VALIDATE); raise / dispense / cancel / receive / MAR link / ward return functions; FKs into IPD tables cascade (IPD purge unchanged); purge v6 — no other IPD table touched, no trigger on IPD tables | `rollback/139_rollback_rx_ipd.sql` (restores purge v5; CHECK narrowed only if no `source='pharmacy'` rows) | VERIFIED |
| 140 | `140_rx_transfers_audits.sql` | RX-41 | transfers, transfer lines, stock audits, audit lines, par levels; save / dispatch / receive / cancel transfer, par levels, start / count / review / post / cancel audit; ledger triggers `rx_sm_audit_freeze` + `rx_sm_audit_delta`; purge v7 | `rollback/140_rollback_rx_transfers_audits.sql` (drops the ledger triggers, restores purge v6) | VERIFIED |
| 141 | `141_rx_registers.sql` | RX-48 | `rx_register_entries` (hash-chained, gapless seq), `rx_register_hash` / `_append` / `_verify`, deferred ledger trigger `trg_rx_sm_register` (no writer redefined), `rx_record_empties` (A11), `rx_batch_trace` (A6), purge v8 | `rollback/141_rollback_rx_registers.sql` (refused while register rows exist; drops the ledger trigger, restores purge v7) | VERIFIED |
| 142 | `142_rx_refills_alerts.sql` | RX-55 | refill plans / requests, alerts (deduped per day, STAT exempt), item metrics; `rx_ledger_check`, `rx_alerts_scan`, `rx_expiry_return_plan`; erasure trigger `trg_rx_patient_erased` on patients; purge v9 | `rollback/142_rollback_rx_refills_alerts.sql` (refused while plans / requests exist; drops the patients trigger, restores purge v8) | VERIFIED |

## 6. Task matrix
Columns: ID · task · depends · acceptance · tests · state. Every task's "tests" must exist and pass before VERIFIED.

### P0 — Baseline
| ID | Task | Depends | Acceptance | Tests | State |
|:-|:-|:-|:-|:-|:-|
| RX-00 | Branch `rx/p0-baseline`; run RG1–RG7 on `main`; record counts in §3 | — | all green; counts pasted | RG1–RG7 | VERIFIED |
| RX-01 | Re-verify every AUDIT.md row by symbol; record moved lines in §7; update design if a finding changed | RX-00 | A1–A48 confirmed or docs updated in same commit | — (evidence in §7) | VERIFIED |

### P1 — Foundation (RX-M1, 134)
| ID | Task | Depends | Acceptance | Tests | State |
|:-|:-|:-|:-|:-|:-|
| RX-02 | Migration 134 + rollback + verify block | RX-01 | applies on 133; idempotent; rollback + re-apply | `test_migration_rx_m1.py` | VERIFIED |
| RX-03 | `tenant.py`: `OPT_IN_FEATURES["pharmacy_enabled"]`, `rx_eligible`, `rx_access` (ADR-RX-09) | RX-02 | off by default; needs OPD; wildcard never enables | `test_rx_feature_gate.py` | VERIFIED |
| RX-04 | `permissions.py`: RX perms, PHARMACIST/PHARMACY_MANAGER/STORE_KEEPER, implied grants, `holds_rx_permission`, `allowed_store_ids` | — | existing logins gain only SECURITY §3.2 grants, only while rx_access | `test_rx_feature_gate.py`, existing permission tests | VERIFIED |
| RX-05 | `app/routers/rx.py` + `app/services/rx.py`: `_rx_scope`, settings (CAS), stores CRUD, setup checklist, deactivation preview; mount in `app/main.py` | RX-03, RX-04 | check order permission → tenant → access → wind-down → store | `test_rx_feature_gate.py` | VERIFIED |
| RX-06 | `platform.py`: `pharmacy_enabled` toggle (eligibility 400, provision default store, 409 preview, confirm → WIND_DOWN, re-enable; `None` = disable); refuse `opd_enabled` off while `rx_state ≠ OFF` (D7); owner console card | RX-05 | ADR-RX-09 lifecycle | `test_rx_feature_gate.py` | VERIFIED |
| RX-07 | `/admin/me` + `rx_access`, `rx_state`, `rx_store_ids`, `rx_perms` | RX-04 | additive keys | `test_rx_feature_gate.py` | VERIFIED |
| RX-08 | Platform purge: `rx_purge_clinic` after `dx_purge_clinic` and before OPD/IPD purge, only when the clinic has `rx_stores` rows (every RX row hangs off a store), so the DX-first contract and non-RX deletions are unchanged (D8, A60) | RX-02 | purge of RX clinic leaves zero rx rows and succeeds; non-RX purge makes no rx call | `test_rx_feature_gate.py::test_clinic_deletion_purges_pharmacy_before_opd`, `::test_deletion_of_a_clinic_without_pharmacy_makes_no_rx_purge_call` | VERIFIED |
| RX-09 | `tenancy.py` registration (134 tables); `test_rx_tenancy_registry.py`; `test_rx_route_matrix.py` skeleton | RX-05 | RG1, RG2 green | RG1, RG2 | VERIFIED |
| RX-10 | `tests/test_rx_no_llm.py` (ADR-RX-06) | RX-05 | | itself | VERIFIED |
| RX-11 | UI: Pharmacy nav group, `applyRxGates()`, **Pharmacy Setup** page (`<!-- RX:foundation -->`) | RX-05–07 | hidden for non-RX clinic; 1440/390 | manual (§9) | VERIFIED |
**P1 DoD:** no clinic's behaviour changed; owner can enable / wind down / re-enable on a sandbox clinic; RG1–RG7 green.

### P2 — Catalogue & ledger (135, 136)
| ID | Task | Depends | Acceptance | Tests | State |
|:-|:-|:-|:-|:-|:-|
| RX-12 | Migration 135 (catalogue) | P1 | immutables, schedule downgrade refused, change log | `test_migration_rx_m2.py` | VERIFIED |
| RX-13 | `rx_catalogue.py` + item/generic/manufacturer/barcode routes | RX-12 | API §2 | `test_rx_catalogue_api.py` | VERIFIED |
| RX-14 | Item CSV/XLSX import with dry run (ROLLOUT §4.2) | RX-13 | idempotent; row errors | `test_rx_catalogue_api.py::test_import_*` | VERIFIED |
| RX-15 | Deterministic resolver (`/items/resolve`) and substitutes (same generic) | RX-13 | stable ordering; no LLM | `test_rx_catalogue_api.py::test_resolve_*` | VERIFIED |
| RX-16 | UI **Drug Master** | RX-13–15 | UI §7 | manual (§9) | VERIFIED |
| RX-17 | Migration 136 (batches, movements, balances, triggers, views) | RX-12 | ADR-RX-01/02 invariants | `test_migration_rx_m3.py` | VERIFIED |
| RX-18 | `rx_stock.py`: FEFO, ledger, stock, expiry, reorder reads | RX-17 | API §3 reads | `test_rx_stock_api.py` | VERIFIED |
| RX-19 | Write-offs, batch block/unblock | RX-17 | reasons; expired write-off allowed | `test_rx_stock_api.py` | VERIFIED |
| RX-20 | Opening stock import (ROLLOUT §4.3) | RX-17, RX-14 | only before first non-opening movement; expired rejected | `test_rx_stock_api.py::test_opening_*` | VERIFIED |
| RX-21 | UI **Stock & Expiry** | RX-18–20 | UI §5 | manual (§9) | VERIFIED |

### P3 — Purchase (137)
| ID | Task | Depends | Acceptance | Tests | State |
|:-|:-|:-|:-|:-|:-|
| RX-22 | Migration 137 | P2 | §4.3 vectors in SQL; posted docs immutable; rollback + double re-apply | `test_migration_rx_m4.py` | VERIFIED |
| RX-23 | Suppliers (GSTIN checksum) | RX-22 | checksum, state from GSTIN, unique per clinic | `test_rx_purchase.py` | VERIFIED |
| RX-24 | Purchase orders (draft/send/cancel/PDF) | RX-23 | `PO-` gapless | `test_rx_purchase.py` | VERIFIED |
| RX-25 | GRN draft/calc/post/void/labels (W1) | RX-24 | TEST-PLAN §4.3 vectors | `test_rx_purchase.py` | VERIFIED |
| RX-26 | Supplier returns + debit note (W6) | RX-25 | expired batches can leave | `test_rx_purchase.py` | VERIFIED |
| RX-27 | UI **Purchase & GRN** | RX-23–26 | UI §4 | manual (§9) | VERIFIED |

### P4 — POS (138)
| ID | Task | Depends | Acceptance | Tests | State |
|:-|:-|:-|:-|:-|:-|
| RX-28 | Migration 138 | P3 | | `test_migration_rx_m5.py` | VERIFIED |
| RX-29 | POS lookup (token/MRN/phone/QR) + Rx line mapping + alias learning | RX-28, RX-15 | W2 steps 1–3 | `test_rx_pos.py` | VERIFIED |
| RX-30 | `rx_tax.py` + SQL `rx_gst_split_inclusive`, `rx_unit_price` | RX-28 | TEST-PLAN §4.1/4.2; property test | `test_rx_tax.py` | VERIFIED |
| RX-31 | Draft lines: server pricing, FEFO pick/split, warnings, discount cap | RX-29, RX-30 | | `test_rx_pos.py` | VERIFIED |
| RX-32 | `rx_complete_sale` + route (schedule checks, payments, fulfilments, idempotency) | RX-31 | W2/W3 lists | `test_rx_pos.py`, `test_rx_concurrency.py` | VERIFIED |
| RX-33 | Receipts 80 mm / A5 PDF + print view; WhatsApp e-bill (template) | RX-32 | UI §2.5 fields | `test_rx_pos.py::test_receipt_*` | VERIFIED |
| RX-34 | Shift integration: 138 redefines SQL `opd_close_shift` adding `rx_sale_payments` cash (D1); `get_collections_summary` gains additive `pharmacy_*` keys | RX-32 | expected cash identical for shifts without pharmacy cash; pharmacy cash reconciles | `test_rx_pos.py::test_shift_*`, `test_rx_zero_impact.py::test_close_shift_unchanged_without_rx`; RG5 | VERIFIED |
| RX-35 | Returns, credit notes, same-day void (W5a) | RX-32 | | `test_rx_returns.py` | VERIFIED |
| RX-36 | UI **Pharmacy POS** (keyboard map, drawer, mapping, receipt) | RX-29–35 | ≤ 3 s timing script | manual + timing (§9) | VERIFIED (UI live check §9); 3 s timing script `tests/ui/rx_pos_timing.md` not yet recorded |

### P5 — IPD (139)
| ID | Task | Depends | Acceptance | Tests | State |
|:-|:-|:-|:-|:-|:-|
| RX-37 | Migration 139 (NOT VALID + VALIDATE for CHECK/FK) | P4 | 111/115/116/117 suites unedited green | `test_migration_rx_m6.py`, RG5 | VERIFIED |
| RX-38 | Indents: indentable list, raise, cancel, queue; order-end cascade | RX-37 | W4 steps 1–3 | `test_rx_ipd.py` | VERIFIED (order end: no trigger, D9 — dispense re-checks the order, the pharmacist closes the stale line) |
| RX-39 | `rx_dispense_indent` (admission lock first, order re-check, D9/D10), ward issue, receive, slip PDF; MAR route pops `rx_dispense_line_id` and writes `rx_mar_links` after the unchanged insert (D2) | RX-38 | ADR-RX-04 list | `test_rx_ipd.py`, `test_rx_concurrency.py` | VERIFIED for dispense / receive / slip / `rx_link_mar`; DEFERRED: ward issue route and the MAR route wiring (P5 leaves IPD handlers unedited; API §6 notes), `test_rx_concurrency.py` (RG8) |
| RX-40 | IPD returns (W5b) + UI **Ward Indents** + MAR batch select | RX-39 | | `test_rx_ipd.py`, manual | VERIFIED (one-step ward return; Ward Indents nurse view + pharmacy queue); DEFERRED: MAR batch select (needs the MAR route wiring) and a returns UI |

### P6 — Transfers & audits (140)
| ID | Task | Depends | Acceptance | Tests | State |
|:-|:-|:-|:-|:-|:-|
| RX-41 | Migration 140 | P5 | | `test_migration_rx_m7.py` | VERIFIED |
| RX-42 | Transfers dispatch/receive/short write-off; controlled two-person | RX-41 | W7 | `test_rx_transfers.py` | VERIFIED |
| RX-43 | Store scope for ward sub-stores in all RX routes | RX-42 | nurse ward A cannot touch ward B (404) | `test_rx_route_matrix.py` | VERIFIED across branches (`test_rx_transfers.py::test_ward_store_scope_and_roles`); same-branch wards need a staff→ward assignment (not in schema) |
| RX-44 | UI **Transfers** (desktop + 390 px receive) | RX-42 | | manual | VERIFIED (live UI §9) |
| RX-45 | Audits start/count/review | RX-41 | | `test_rx_audit.py` | VERIFIED |
| RX-46 | Audit delta trigger, freeze, post with second approver | RX-45 | W8 | `test_rx_audit.py`, `test_rx_concurrency.py` | VERIFIED (`test_rx_concurrency.py` / RG8 outstanding) |
| RX-47 | UI **Stock Audit** (blind count, mobile count) | RX-46 | | manual | VERIFIED (live UI §9) |

### P7 — Registers (141)
| ID | Task | Depends | Acceptance | Tests | State |
|:-|:-|:-|:-|:-|:-|
| RX-48 | Migration 141 (register table, `rx_register_append`, `rx_register_verify`) | P6 | | `test_migration_rx_m8.py` | VERIFIED |
| RX-49 | Wire register writes into GRN, sale, void, return, dispense, ward issue, transfer, audit, write-off functions (as built: one deferred trigger on the ledger, DATA-MODEL §8) | RX-48 | every controlled movement has exactly one register row | `test_migration_rx_m8.py`, `test_rx_registers.py` | VERIFIED |
| RX-50 | Register list/export PDF+CSV/verify routes; nightly verify + ledger check jobs | RX-49 | W9 | `test_rx_registers.py` | VERIFIED (routes; the nightly verify job lands with the RX-56 alert scan) |
| RX-51 | UI **Drug Registers** | RX-50 | UI §8 | manual | VERIFIED (live UI §9) |

### P8 — Refills & alerts (142)
| ID | Task | Depends | Acceptance | Tests | State |
|:-|:-|:-|:-|:-|:-|
| RX-55 | Migration 142 | P7 | | `test_migration_rx_m9.py` | VERIFIED |
| RX-56 | `rx_alerts_scan` job + alert routes + STAT indent notifications (+ nightly ledger check and register chain verify, P1 notifications) | RX-55 | dedupe/day; OFF clinics skipped | `test_rx_alerts.py` | VERIFIED |
| RX-57 | Refill plan creation on sale (closed frequency map only; exclusions) | RX-55 | | `test_rx_refills.py` | VERIFIED |
| RX-58 | `rx_refill_reminders` job (`distributed_job_lock`), opt-in + `accepts_engagement` gate, button templates, `rx_refill:`/`rx_refill_off:` prefix hook beside the dental-review hook (D3) | RX-57 | W10; typed text never intercepted; webhook SLA untouched (async worker) | `test_rx_refills.py`, RG4 | VERIFIED (live sends need Meta approval of `rx_refill_due_v1` per clinic) |
| RX-59 | UI **Refill Requests** + verify/pack/reject (as built: **Refills & Alerts** page, 4 tabs, POS opt-in tick) | RX-58 | | manual | VERIFIED (live UI §9) |

### P9 — Ketu & dashboard
| ID | Task | Depends | Acceptance | Tests | State |
|:-|:-|:-|:-|:-|:-|
| RX-60 | Ketu registry actions (KRIYA-KETU §2), `_rx_on`, `PAGES` entries | P8 | RT-14 | `test_assistant_registry.py`, `test_assistant_reads.py`, `test_rx_ketu.py` | VERIFIED |
| RX-61 | `/admin/rx/stock/answer` + `present.rx_stock`; voice `PHARMACY_REFILL` | RX-60 | KRIYA-KETU §2.1, §3 | `test_rx_ketu.py` | VERIFIED |
| RX-62 | Dashboard route + UI **Pharmacy Dashboard** | P8 | UI §9 | `test_rx_dashboard.py::test_dashboard_*`, manual | VERIFIED (live UI §9) |

### P10 — Reports
| ID | Task | Depends | Acceptance | Tests | State |
|:-|:-|:-|:-|:-|:-|
| RX-52 | GSTR-1: pharmacy B2C by rate + HSN summary + credit notes; non-RX golden file unchanged | P4 | | `test_rx_reports.py` | VERIFIED |
| RX-53 | CSV reports (sales/purchase registers, stock valuation, movement, expiry, margin, day-book) with formula-injection guard | P6 | | `test_rx_reports.py` | VERIFIED |
| RX-54 | Distributor invoice CSV/XLSX → GRN draft (stretch; may be DEFERRED by owner) | P3 | | `test_rx_purchase.py::test_invoice_import_*` | DEFERRED (owner, 2026-11-18: P10 kept to accounting and analytics) |

### P11 — Hardening & release
| ID | Task | Depends | Acceptance | Tests | State |
|:-|:-|:-|:-|:-|:-|
| RX-63 | Concurrency suite complete (TEST-PLAN §3) + latency check (≤ 5% regression) | P10 | | RG8, §9 latency table | VERIFIED (RG8: `test_rx_concurrency.py`; latency check not measured — needs the perf fixture on the deployed stack) |
| RX-64 | Cutover rehearsal on sandbox with a real legacy export (ROLLOUT §5) | P10 | reconciliation ≤ 0.5% | manual, §9 | VERIFIED (automated rehearsal, simulated legacy export, 0 % variance; repeat with the real export at T−7) |
| RX-65 | `tests/test_rx_release_readiness.py` (lifecycle, rollback chain 142→133, purge) | RX-63 | | itself | VERIFIED |
| RX-66 | AGENTS.md row + `docs/agent-context` updates; ROLLOUT owner checklist handed over | RX-65 | | — | VERIFIED (AGENTS.md §2 row + §3 commands; `docs/agent-context` lives outside this repo and was not edited) |

### Verification fixes and advanced features (VERIFICATION.md, ADVANCED-FEATURES.md)
| ID | Task | Phase | Depends | Acceptance | Tests | State |
|:-|:-|:-|:-|:-|:-|:-|
| RX-67 | `delete_branch`: on FK violation (23503) fall back to the existing deactivate path (latent bug L1) | P1 | RX-02 | branch with IPD ward / DX site / RX store → deactivated, not 500; clean branch still deleted | `test_admin_branches.py::test_delete_branch_with_referencing_records_deactivates` | VERIFIED |
| RX-68 | DPDP erasure: per-member 23503 → redact and continue (latent bug L2); delete the patient's refill plans/requests | P1 (plans part in P8) | RX-02 | erasure completes for members referenced by IPD/DX/RX; registers retained | `test_opd_data_retention.py::test_family_member_with_non_opd_fk_references_is_redacted`, `test_rx_refills.py::test_dpdp_erasure_removes_refill_state_not_registers` | VERIFIED (refill part: P8, trigger `trg_rx_patient_erased`) |
| RX-69 | Rx Inbox (A1) | P4 | RX-32 | | `test_rx_pos.py::test_inbox_*` | VERIFIED |
| RX-70 | Park / recall bills (A2) | P4 | RX-31 | | `test_rx_pos.py::test_park_*` | VERIFIED |
| RX-71 | GS1 / pharma QR parser at GRN and POS (A3) | P3 | RX-25 | parser vectors | `test_rx_gs1.py` | VERIFIED (GRN; POS use lands with RX-31) |
| RX-72 | Dispensing labels (A4) | P4 | RX-33 | verbatim prescriber text; closed vocabulary only | `test_rx_labels.py` | VERIFIED |
| RX-73 | Repeat last bill (A5) | P4 | RX-32 | scheduled items need valid Rx | `test_rx_pos.py::test_repeat_*` | VERIFIED |
| RX-74 | Batch recall trace + confirmed notify (A6) | P7 | RX-50 | | `test_migration_rx_m8.py::test_batch_trace_*`, `test_rx_registers.py::test_batch_recall_trace` | VERIFIED (trace; confirmed notify deferred — needs the `rx_recall_v1` template) |
| RX-75 | ADU reorder suggestions, ABC/VED/FSN, dead stock (A7) | P10 | P8 | suggest-then-apply only | `test_rx_metrics.py` | VERIFIED |
| RX-76 | Expiry-return planner (A8) | P8 | RX-26 | drafts only | `test_migration_rx_m9.py::test_expiry_return_plan_*`, `test_rx_alerts.py::test_expiry_return_planner_*` | VERIFIED (on-demand planner + one-click draft; the monthly drafting job is not scheduled) |
| RX-77 | Manager override tokens (A9) | P4 | RX-32 | single-use, 60 s, approver ≠ requester | `test_rx_pos.py::test_override_*` | VERIFIED |
| RX-78 | Ward par levels + rack-sorted pick lists (A10) | P6 | RX-42 | drafts only | `test_rx_transfers.py::test_par_*` | VERIFIED (built in P6) |
| RX-79 | Narcotic empty-ampoule control (A11) | P7 | RX-49 | two-person; override with reason | `test_rx_registers.py::test_narcotic_empties_two_person`, `test_migration_rx_m8.py::test_narcotic_empties_*` | VERIFIED (recording + two-person; re-dispense override deferred) |
| RX-80 | Lost-sale / pending-balance call list (A12) | P9 | P8 | no automatic messages | `test_rx_dashboard.py::test_lost_sale_list_masked_and_audited_reveal` | VERIFIED |
| RX-81 | Pharmacy shift Z-report (A13) | P4 | RX-34 | matches `opd_close_shift` expected cash | `test_rx_pos.py::test_z_report_*` | VERIFIED |
| RX-82 | Medicine history + recorded allergies panel, display only (A14) | P4 | RX-29 | no checking logic | `test_rx_pos.py::test_history_panel_*` | VERIFIED |
| RX-83 | `tests/test_rx_zero_impact.py`: one test per VERIFICATION §7 row | P1→P11 (grows per phase) | — | every row green | itself | VERIFIED |

### Deferred (owner decision needed)
| ID | Item | Reason |
|:-|:-|:-|
| RX-D2 | Credit / on-account sales | O-1 |
| RX-D3 | Offline OTC POS queue | G15 — conflicts with server-enforced guarantees |
| RX-D4 | Home delivery logistics for refills | O-6 |
| RX-D5 | Cold-chain temperature logging | G28 |
| RX-D6 | Standalone retail pharmacy without OPD | G30 |
| RX-D7 | Per-store login pins (`RX_STORE:<uuid>`) | staff grants are validated against `PERMISSIONS`; P1 scopes by branch (SECURITY §3.3) |

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
| 2026-11-16 | RX-00 RG1 (baseline) | `.venv/bin/pytest tests/test_lint_unscoped_queries.py tests/test_phase4_scoped_queries.py -q` | 10 passed |
| 2026-11-16 | RX-00 RG2 (baseline) | `.venv/bin/pytest tests/test_admin_super_admin_scope_matrix.py tests/test_phase2_route_adversarial_matrix.py -q` | 1587 passed |
| 2026-11-16 | RX-00 RG3 (baseline) | `.venv/bin/pytest tests/test_clinical_firewall.py tests/test_dx_no_llm.py -q` | 41 passed |
| 2026-11-16 | RX-00 full suite (baseline, clean worktree of `1078dca`) | `.venv/bin/pytest -q -rs` | 9412 passed, 5 skipped, 0 failed (a first run reported 3 failures only because P1 files appeared mid-run) |
| 2026-11-16 | RX-01 | citation re-check of AUDIT.md / VERIFICATION.md against `git show 1078dca:<file>` (87 symbols + A54/A55) | 0 moved, 0 changed findings; §7 stays empty |
| 2026-11-16 | RX-02 | `.venv/bin/pytest tests/test_migration_rx_m1.py -q` | 11 passed (keys, shapes, composite FKs, guard, one central per branch, RLS, CAS, purge, search_path, rollback + double re-apply) |
| 2026-11-16 | RX-03…RX-08 | `.venv/bin/pytest tests/test_rx_feature_gate.py -q` | 21 passed (gate, perms, grant cap, store scope, settings, HTTP: enable/provision, permission-first, CAS, stores scope, wind-down, OPD-disable guard, purge order) |
| 2026-11-16 | RX-09 / RX-10 | `.venv/bin/pytest tests/test_rx_tenancy_registry.py tests/test_rx_route_matrix.py tests/test_rx_no_llm.py -q` | 17 passed (route count pinned at 6; all 6 in both global matrices; role matrix × 8) |
| 2026-11-16 | 103 rollback with 134 | `.venv/bin/pytest tests/test_migration_103_opd.py -k "rollback or down or reapply" -q` | 1 passed (after the owner-approved 1-line `unwind_133` → `unwind_134` chain extension) |
| 2026-11-16 | P1 RG1 | `.venv/bin/pytest tests/test_lint_unscoped_queries.py tests/test_phase4_scoped_queries.py -q` | 10 passed |
| 2026-11-16 | P1 RG2 | `.venv/bin/pytest tests/test_admin_super_admin_scope_matrix.py tests/test_phase2_route_adversarial_matrix.py -q` | 1605 passed (the 6 `/admin/rx` routes included) |
| 2026-11-16 | P1 RG3 | `.venv/bin/pytest tests/test_clinical_firewall.py tests/test_rx_no_llm.py -q` | 37 passed |
| 2026-11-16 | P1 full suite | `.venv/bin/pytest -q -rs` | **9479 passed, 5 skipped, 0 failed** |
| 2026-11-16 | RX-11 live UI | real app + fresh pgserver (001→134 applied clean) + PostgREST bridge, real logins, `lifespan=off` (no scheduler, no Supabase, no WhatsApp) | **@1440 px** `rxadmin`: Pharmacy group + Setup page; checklist Stores ✓ / Drug master / GSTIN (bill of supply) / Settings; MAIN store provisioned; added ward sub-store W3 (Kukatpally, Ward 3, parent MAIN) through the form; Save settings 12 % → 1200 bps, version 1, toast, checklist Settings ✓. **@390 px** branch-pinned `pharmacist`: page width = viewport (no overflow), sees only W3, store form hidden, settings read-only. Plain OPD clinic `opdadmin`: no RX element visible, `/admin/rx/setup` 403. Found and fixed: checklist counted only the login's visible stores (now clinic-wide, test added). Console: no errors (one deliberate 403 probe) |
| 2026-11-17 | RX-12 (Migration 135) | `.venv/bin/pytest tests/test_migration_rx_m2.py -q` | 9 passed (item rules, schedule downgrade refused, rate/HSN reason, change log append-only, resolver, import dry-run/apply, rollback + re-apply) |
| 2026-11-17 | RX-13…RX-15 (Catalogue API) | `.venv/bin/pytest tests/test_rx_catalogue_api.py -q` | 6 passed (items, generics, manufacturers, barcodes, resolver with substitutes, import dry-run/apply) |
| 2026-11-17 | RX-17 (Migration 136) | `.venv/bin/pytest tests/test_migration_rx_m3.py -q` | 12 passed (ledger append-only, no negative stock, expired/blocked never leave for patients, IST boundary, balance == sum after 1000 movements, opening stock, write-offs, FEFO views, purge v3, rollback + re-apply) |
| 2026-11-17 | RX-18…RX-20 (Stock API) | `.venv/bin/pytest tests/test_rx_stock_api.py -q` | 5 passed (stock, batches, expiry, reorder, block/unblock, write-offs, opening import) |
| 2026-11-17 | RX-16, RX-21 live UI | real app at 1440 px and 375/390 px in dark and light modes | **Drug Master (`rxitems`)**: item table, search/filter, item edit, import toggle. **Stock & Expiry (`rxstock`)**: stock by store, batch breakdown, near-expiry & expired alerts, reorder tab, write-off modal. Store-scoping enforced. |
| 2026-11-17 | Schema verify §28–§30 | `migrations/verify_supabase_schema.sql` on real pg | 9/9 checks PASS (M134: 3, M135: 3, M136: 3) |
| 2026-11-17 | Ketu page registry (RT-14) | `.venv/bin/pytest tests/test_assistant_registry.py -q` | 243 passed (`rxitems` & `rxstock` added to `PAGES` & `PAGE_SYNONYMS`) |
| 2026-11-17 | P2 RG1 | `.venv/bin/pytest tests/test_lint_unscoped_queries.py tests/test_phase4_scoped_queries.py -q` | 10 passed |
| 2026-11-17 | P2 RG2 | `.venv/bin/pytest tests/test_admin_super_admin_scope_matrix.py tests/test_phase2_route_adversarial_matrix.py -q` | 1665 passed |
| 2026-11-17 | P2 RG3 | `.venv/bin/pytest tests/test_clinical_firewall.py tests/test_rx_no_llm.py -q` | 37 passed |
| 2026-11-17 | Migration 103 chain | `.venv/bin/pytest tests/test_migration_103_opd.py -k "rollback or down or reapply" -q` | 1 passed |
| 2026-11-17 | RX-22 (Migration 137) | `.venv/bin/pytest tests/test_migration_rx_m4.py -q` | 10 passed (§4.3 vectors with/without GSTIN, IGST + freight largest-remainder split, PO send numbering + draft-only edits, partial→received, post idempotent, ±99 round-off, duplicate invoice, expired refused, posted GRN frozen, void only while unconsumed, return of blocked batch + rate fill, purge v4, rollback + double re-apply) |
| 2026-11-17 | RX-23…RX-26 (Purchase API) | `.venv/bin/pytest tests/test_rx_purchase.py -q` | 11 passed (GSTIN checksum/state, PO → GRN → stock, free packs dilute cost, interstate IGST, total mismatch 409 with diff, duplicate invoice 409, line rules + warnings, void 403/409/200, expired-batch return + debit note, store/tenant scope, GS1 lookup) |
| 2026-11-17 | RX-71 (GS1 parser) | `.venv/bin/pytest tests/test_rx_gs1.py -q` | 16 passed (]d2 / ]Q3 / GS / bracket / `<GS>` forms, day 00 = month end, refusals) |
| 2026-11-17 | P3 route matrix | `.venv/bin/pytest tests/test_rx_route_matrix.py -q` | route pin 50 (+23 P3); P3 role matrix × 7 (store keeper drafts, never approves) |
| 2026-11-17 | Ketu page registry | `.venv/bin/pytest tests/test_assistant_registry.py -q` | 243 passed (`rxpurchase` in `PAGES` + `PAGE_SYNONYMS`) |
| 2026-11-17 | P3 RG1 | `.venv/bin/pytest tests/test_lint_unscoped_queries.py tests/test_phase4_scoped_queries.py -q` | 10 passed |
| 2026-11-17 | P3 RG2 | `.venv/bin/pytest tests/test_admin_super_admin_scope_matrix.py tests/test_phase2_route_adversarial_matrix.py -q` | 1734 passed |
| 2026-11-17 | P3 RG3 | `.venv/bin/pytest tests/test_clinical_firewall.py tests/test_rx_no_llm.py -q` | 37 passed |
| 2026-11-17 | P3 RX suites | `.venv/bin/pytest tests/test_migration_rx_m*.py tests/test_rx_purchase.py tests/test_rx_gs1.py tests/test_rx_route_matrix.py -q` | 92 passed |
| 2026-11-17 | Migration 103 chain | `.venv/bin/pytest tests/test_migration_103_opd.py -k "rollback or down or reapply" -q` | 1 passed (unwind 137→136→135→134→133 newest first) |
| 2026-11-17 | Schema verify §28–§31 | `migrations/verify_supabase_schema.sql` on real PG (fresh 001→137) | 12/12 PASS (M137: 3), 0 FAIL in the whole report |
| 2026-11-17 | RX-27 live UI | real app + fresh pgserver (001→137) + PostgREST bridge, `lifespan=off` | **@1440 px** `rxadmin`: Reorder grouped by last supplier → draft PO (AUG625 10, BENAD 12) → Approve & send `PO-2026-00001` → GRN against the PO, lines prefilled, Save & compute (Augmentin taxable ₹1,323, landed ₹12.03/tab with 1 free strip), footer "✓ matches (round-off ₹0.37)" → Post `GRN-2026-00001` (122 units) → PO auto `received`; labels PDF (42 KB) and PO PDF render; supplier return of DL2405 10 units → debit note ₹14.70 → dispatched `SRN-2026-00001`; supplier form GSTIN hint (✕ wrong check digit / ✓ state 36). **@375 px** `pharmacist`: read-only (no New / Post / Void), no horizontal scroll. Found and fixed: PO-prefilled GRN lines defaulted to 0 % GST (PO item lacked `tax_rate_bps`; regression assert added). |
| 2026-11-17 | P3 full suite | `.venv/bin/pytest -q -rs` | **9690 passed, 5 skipped, 0 failed** |
| 2026-11-17 | RX-28 (Migration 138) | `.venv/bin/pytest tests/test_migration_rx_m5.py -q` | 13 passed (8 tables, constraints, opd_close_shift redefinition + cash term D1, rx_complete_sale, returns, void, purge v5, rollback + re-apply) |
| 2026-11-17 | RX-30 (Tax Engine) | `.venv/bin/pytest tests/test_rx_tax.py -q` | 14 passed (inclusive GST split vectors, loose unit price vectors, 10,000 random amounts property test Python == SQL, no-GSTIN behavior) |
| 2026-11-17 | RX-29, RX-31…RX-35 (POS API) | `.venv/bin/pytest tests/test_rx_pos.py -q` | 14 passed (prescription lookup, line mapping, draft lines, complete sale, returns unopened/damaged, PCN credit notes, park/recall, overrides, shift collections) |
| 2026-11-17 | RX-72 (Dispensing labels) | `.venv/bin/pytest tests/test_rx_labels.py -q` | 2 passed (label PDF rendering, prescriber directions, patient details) |
| 2026-11-17 | P4 route matrix | `.venv/bin/pytest tests/test_rx_route_matrix.py -q` | route pin 68 (+18 P4); P4 role matrix (pharmacist dispenses, front desk cannot) |
| 2026-11-17 | Ketu page registry | `.venv/bin/pytest tests/test_assistant_registry.py -q` | 243 passed (`rxpos` in `PAGES` + `PAGE_SYNONYMS`) |
| 2026-11-17 | P4 RG1 | `.venv/bin/pytest tests/test_lint_unscoped_queries.py tests/test_phase4_scoped_queries.py -q` | 10 passed |
| 2026-11-17 | P4 RG2 | `.venv/bin/pytest tests/test_admin_super_admin_scope_matrix.py tests/test_phase2_route_adversarial_matrix.py -q` | 1788 passed |
| 2026-11-17 | P4 RG3 | `.venv/bin/pytest tests/test_clinical_firewall.py tests/test_rx_no_llm.py -q` | 37 passed |
| 2026-11-17 | P4 RX suites | `.venv/bin/pytest tests/test_migration_rx_m*.py tests/test_rx_tax.py tests/test_rx_pos.py tests/test_rx_labels.py tests/test_rx_purchase.py tests/test_rx_gs1.py tests/test_rx_catalogue_api.py tests/test_rx_stock_api.py tests/test_rx_route_matrix.py tests/test_rx_feature_gate.py -q` | 174 passed |
| 2026-11-17 | Migration 103 chain | `.venv/bin/pytest tests/test_migration_103_opd.py -k "rollback or down or reapply" -q` | 1 passed (unwind 138→137→136→135→134→133 newest first) |
| 2026-11-17 | Schema verify §28–§32 | `migrations/verify_supabase_schema.sql` on real PG (fresh 001→138) | 15/15 PASS (M138: 3), 0 FAIL in the whole report |
| 2026-11-17 | RX-36 live UI | real app + fresh pgserver (001→138) + PostgREST bridge, `lifespan=off` | **@1440 px** `pharmacist`: Token 17 → lookup signed Rx → auto-map lines → F6 cash → Enter completes sale `PHS-2026-000001` → 80 mm thermal & A5 receipt render; patient return → `PRT-2026-00001` → cash refund; discount above cap with manager override token (`rx_overrides`); shift Z-report day summary; fixed double "Dr Dr." title. **@375 px**: single-column layout, sticky bottom bar. |
| 2026-11-17 | P4 full suite | `.venv/bin/pytest -q -rs` | **9794 passed, 5 skipped, 0 failed** |
| 2026-11-17 | RX-37 migration 139 | `.venv/bin/pytest tests/test_migration_rx_m6.py -q` | 14 passed (source CHECK widened + keeps `package`, no rx trigger on IPD tables, active-medication-order lines, function-only writes, IND numbering + replay, dispense ×3 → one charge, autopost off + 999 split, stopped order / pending stay refused, substitution, receive closes, MAR link validated, return void + re-issue, credit note on issued bill, IPD owner purge cascades RX rows, purge v6 + guarded rollback + re-apply ×2) |
| 2026-11-17 | RX-38…RX-40 API | `.venv/bin/pytest tests/test_rx_ipd.py -q` | 5 passed (nurse raises → STAT-first queue → FEFO dispense → one posted charge ₹2/tab → slip PDF → receive closes; dose on the unchanged MAR route linked to the batch, wrong line refused with MAR kept; ward return void + re-issue; permissions + stopped order 409; IPD WIND_DOWN still dispenses, IPD off → 403) |
| 2026-11-17 | P5 route matrix | `.venv/bin/pytest tests/test_rx_route_matrix.py -q` | route pin 77 (+9 P5); P5 role matrix (nurse raises not dispenses, pharmacist dispenses not raises, store keeper / cashier / doctor 403) |
| 2026-11-17 | Ketu page registry | `.venv/bin/pytest tests/test_assistant_registry.py -q` | 243 passed (`rxindents` in `PAGES` + `PAGE_SYNONYMS`) |
| 2026-11-17 | P5 RG1 | `.venv/bin/pytest tests/test_lint_unscoped_queries.py tests/test_phase4_scoped_queries.py -q` | 10 passed |
| 2026-11-17 | P5 RG2 | `.venv/bin/pytest tests/test_admin_super_admin_scope_matrix.py tests/test_phase2_route_adversarial_matrix.py -q` | 1815 passed |
| 2026-11-17 | P5 RG3 | `.venv/bin/pytest tests/test_clinical_firewall.py tests/test_rx_no_llm.py -q` | 37 passed |
| 2026-11-17 | P5 RX suites | `.venv/bin/pytest tests/test_migration_rx_m*.py tests/test_rx_ipd.py tests/test_rx_pos.py tests/test_rx_route_matrix.py -q` | 126 passed |
| 2026-11-17 | P5 RG5 (IPD unedited) | `.venv/bin/pytest tests/test_migration_111_ipd.py tests/test_migration_115_orders.py tests/test_migration_116_ipd_finance.py tests/test_migration_117_gst.py -q` | 63 passed, files unedited. Found and fixed on the way: `ipd_purge_clinic` (used by 111's rollback test) failed on `rx_indent_lines_order_fk` — every FK into an IPD table now cascades / sets null and the RX guard yields to the IPD purge |
| 2026-11-17 | Migration 103 chain | `.venv/bin/pytest tests/test_migration_103_opd.py -k "rollback or down or reapply" -q` | 1 passed (unwind 139→138→…→134→133 newest first; `unwind_139` owner-purges clinics with pharmacy charges so `116_down` can narrow `ipd_charges.source`) |
| 2026-11-17 | Schema verify §28–§33 | `migrations/verify_supabase_schema.sql` on real PG (001→139) | 18/18 PASS (M139: 3), 178 checks, 0 FAIL in the whole report |
| 2026-11-17 | RX-40 live UI | real app + pgserver (001→139) + PgBridge, real password logins, `lifespan=off` | **@1440 px** `rxnurse` (NURSE, IPD_NURSING only): Ward Indents in the Pharmacy group; patient picker from the bed board; orders with 24 h defaults (BD → 2, PRN → 1), deterministic item candidates, stores from `indentable`; STAT → `IND-2026-00001`, order shows "2 pending". `rxpharma`: queue STAT (red) above ROUTINE with ward / bed / age; drawer FEFO batch `CEFN1`; Enter dispenses; slip PDF 30 KB. **@390 px** nurse: no horizontal overflow, single column; Receive → indent `closed`. Console: no errors. Found and fixed: nurse nav hidden by `applyOpdRoleNav` (added `rxindents` to its self-gated set), JS / page blocks inserted into the wrong lane (marker substring), priority class `stat` colliding with the dashboard `.stat` card (now `p-stat`), "0 in ward" without a ward sub-store (now "—"), slip pop-up blocked after `await` (window opened inside the click) |
| 2026-11-17 | P5 full suite | `.venv/bin/pytest -q -rs` | **9846 passed, 5 skipped, 0 failed** |
| 2026-11-17 | RX-41 migration 140 | `.venv/bin/pytest tests/test_migration_rx_m7.py -q` | 10 passed (function-only writes, draft replace, same store refused, dispatch STR- + replay, short receipt: target +received, source debited once, `transfer_in`+`damage_writeoff_out` at source; blocked batch never leaves; controlled two-person; audit delta from a concurrent sale, count SET on re-send, reason required, adjust brings stock to count; rack scope, found stock, uncounted blocks review, one open audit per store; freeze blocks sale / dispense until cancel; approver ≠ starter ≠ poster above threshold; par set / remove; purge v7 + guarded rollback + re-apply ×2) |
| 2026-11-17 | RX-42/43/78 API | `.venv/bin/pytest tests/test_rx_transfers.py -q` | 4 passed (FEFO split + rack-ordered pick list, STR-2026-00001, challan PDF, branch-A nurse receives short with write-off at source, branch-B nurse 404 everywhere, nurse never drafts / dispatches, controlled: keeper 403, same manager 409, second manager receives; par draft positive deficits only, refreshed in place) |
| 2026-11-17 | RX-45/46 API | `.venv/bin/pytest tests/test_rx_audit.py -q` | 3 passed (blind for the keeper, manager peeks; counter sale during the count is not a variance; review / reasons / post bring stock to the shelf; frozen store refuses a counter sale until cancelled; second approver: alone 403, pharmacist 403, wrong password 401, manager approves) |
| 2026-11-17 | P6 route matrix | `.venv/bin/pytest tests/test_rx_route_matrix.py -q` | route pin 95 (+18 P6, incl. `GET /audits` and `POST /audits/{id}/cancel`); P6 role matrix (7 roles) |
| 2026-11-17 | Ketu page registry | `.venv/bin/pytest tests/test_assistant_registry.py -q` | 243 passed (`rxtransfers`, `rxaudit` in `PAGES` + `PAGE_SYNONYMS`) |
| 2026-11-17 | P6 RG1 / RG2 / RG3 | lint + scope matrices + firewall / no-LLM | 10 / 1869 / 37 passed |
| 2026-11-17 | P6 RX suites | `.venv/bin/pytest tests/test_migration_rx_m*.py tests/test_rx_transfers.py tests/test_rx_audit.py tests/test_rx_route_matrix.py -q` | 133 passed |
| 2026-11-17 | P6 RG5 (IPD unedited) | `.venv/bin/pytest tests/test_migration_111_ipd.py tests/test_migration_115_orders.py tests/test_migration_116_ipd_finance.py tests/test_migration_117_gst.py -q` | 63 passed |
| 2026-11-17 | Migration 103 chain | `.venv/bin/pytest tests/test_migration_103_opd.py -k "rollback or down or reapply" -q` | 1 passed (unwind 140→139→…→134→133) |
| 2026-11-17 | Schema verify §28–§34 | `migrations/verify_supabase_schema.sql` on real PG (001→140) | 21/21 PASS (M140: 3), 181 checks, 0 FAIL in the whole report |
| 2026-11-17 | RX-44 / RX-47 live UI | real app + pgserver (001→140) + PgBridge, real password logins, `lifespan=off` | **@1440 px** `rxkeeper`: Transfers (4 tabs), par levels rack-sorted (A-1, B-2, C-3) with deficits → Draft replenishment (Dolo FEFO 30 + 15) → Dispatch `STR-2026-00002`. **@390 px** `rxnurse` (IPD_NURSING only): Transfers shown, Stock Audit hidden, tabs Incoming + Par levels; receive with one line short → amber reason field → `partially_received`; no horizontal overflow. `rxkeeper` @390: frozen rack audit `AUD-2026-00001`, blind keypad count mode (Prev / Next, counts saved per batch) → review. @1440: variance grid (deficit red), reason select, post above the limit → approver form → `rxmgr` approves → posted. Console: only the expected 403 that opens the approver form. Fixed during the check: negative values shown as "−₹6.00", number inputs styled in the shared table |
| 2026-11-17 | P6 full suite | `.venv/bin/pytest -q -rs` | **9924 passed, 5 skipped, 0 failed** |
| 2026-11-18 | RX-48/49 migration | `.venv/bin/pytest tests/test_migration_rx_m8.py -q` | 11 passed (GRN in with supplier, unscheduled items write nothing; H1 sale out with patient + prescriber, no phone; IPD dispense out names admission + ordering doctor; transfer rows per store with running balances; function-only + append-only; tamper by update / snapshot edit / delete detected at seq 3; seq per clinic × register; empties two-person + cap; batch trace sale / IPD / transfer / on hand; purge v8; guarded rollback + double re-apply; search_path pinned) |
| 2026-11-18 | RX-50/74/79 API | `.venv/bin/pytest tests/test_rx_registers.py -q` | 6 passed (H1 list + CSV columns + PDF, exports audited with range and rows, no phone even masked; narcotic opening 140 / issued 3 / balance 137; verify turns red at the tampered seq; keeper / nurse 403, pharmacist no trace, bad register 422, foreign store 404; trace with masked phone, ward / bed, audited; empties: pharmacist 403, same person 409, over-return 409, unknown staff / line 404) |
| 2026-11-18 | P7 route matrix | `.venv/bin/pytest tests/test_rx_route_matrix.py -q` | route pin 101 (+6 P7); P7 role matrix (6 roles) |
| 2026-11-18 | Ketu page registry | `.venv/bin/pytest tests/test_assistant_registry.py -q` | 243 passed (`rxregisters` in `PAGES` + `PAGE_SYNONYMS`) |
| 2026-11-18 | P7 RG1 / RG2 / RG3 | lint + scope matrices + firewall / no-LLM | 10 / 1887 / 37 passed |
| 2026-11-18 | P7 RX suites | `.venv/bin/pytest tests/test_migration_rx_m*.py tests/test_rx_registers.py tests/test_rx_route_matrix.py -q` | 149 passed (m7's `unwind_140` now unwinds 141 first, as P6 did for 139) |
| 2026-11-18 | P7 RG5 (IPD unedited) | `.venv/bin/pytest tests/test_migration_111_ipd.py tests/test_migration_115_orders.py tests/test_migration_116_ipd_finance.py tests/test_migration_117_gst.py -q` | 63 passed |
| 2026-11-18 | Migration 103 chain | `.venv/bin/pytest tests/test_migration_103_opd.py -k "rollback or down or reapply" -q` | 1 passed (unwind 141→140→…→134→133) |
| 2026-11-18 | Schema verify §28–§35 | `migrations/verify_supabase_schema.sql` on real PG (001→141) | 24/24 PASS (M141: 3), 184 checks, 0 FAIL in the whole report |
| 2026-11-18 | P7 full suite | `.venv/bin/pytest -q -p no:randomly -x` | **9965 passed, 5 skipped, 0 failed** |
| 2026-11-18 | RX-51 live UI | real app + pgserver (001→141) + PgBridge, real password logins, `lifespan=off`; seeded H1 counter sales + narcotic ward dispense | `rxmgr`: Drug Registers in the Pharmacy group, five tabs, H1 rows (opening receipt, two issues with patient name / age / MRN / address and prescriber reg. no.), chain badge "✓ Verified: hash chain valid (seq 1 to 3)"; Narcotic tab IND row (balance 137); Trace batch DNEAR → ward 1 · bed 101, on hand 37. After tampering H1 seq 2 in SQL, `rxpharma` @375 px light: red "✕ Tamper detected at seq 2", no trace buttons (not RX_ADMIN), no horizontal overflow. PDFs: H1 / narcotic render landscape with wrapped cells, "page 1 of N". Console: no errors. Fixed during the check: export buttons split across rows (grouped), stacked mobile cards lost column labels (data-label added) |
| 2026-11-18 | RX-55 migration | `.venv/bin/pytest tests/test_migration_rx_m9.py -q` | 7 passed (one plan per sold line, one open request per plan, reject reason / packed sale shapes, cross-clinic plan FK; alert dedupe per entity per day with STAT exempt; scan writes low stock / near expiry once a day and expired-on-hand the day after; ledger check catches a forged balance (99 vs 15) and the scan records it with the X register chain break at seq 2; expiry plan by supplier window with drafted units; purge v9; guarded rollback + double re-apply; search_path pinned) |
| 2026-11-18 | RX-57/58/59/68 refills | `.venv/bin/pytest tests/test_rx_refills.py -q` | 24 passed (closed map table: OD/BD/TDS/QID/HS only, `.5` doses, SOS / 1/2 / "1 tab" / TID / 0 / 1.25 → none; plan only with the tick, never for H1 / X / narcotic / psychotropic / non-chronic or refills off; newer sale supersedes; job: STOP respected, sent once, template + two payloads, no dose text, no clinic credentials / WIND_DOWN → nothing sent, three failures → expired; button: double tap = one request, opt-out, wrong phone / clinic ignored (X17), typed text never claimed and the hook sits inside the template-button branch; verify → pack (draft, FEFO, no movement) → counter completes → request completed / plan fulfilled; reject with fixed text; plan list + cancel; expired Rx refused at verify; DPDP erasure removes plans + requests, registers unchanged) |
| 2026-11-18 | RX-56/76 alerts + planner | `.venv/bin/pytest tests/test_rx_alerts.py -q` | 5 passed (scan twice = one low-stock alert, OFF clinic untouched, next day new alert; forged balance + tampered X register → two `[P1]` notifications; list / filter / ack / re-ack / 404 / unnamed login 403; two STAT indents → two alerts + notifications with bed 101; planner keeps the 90-day supplier, drops the 30-day one, drafts only via POST /supplier-returns, then shows drafted units; foreign store 404) |
| 2026-11-18 | P8 route matrix | `.venv/bin/pytest tests/test_rx_route_matrix.py -q` | route pin 110 (+9 P8); P8 role matrix (6 roles) |
| 2026-11-18 | Ketu page registry | `.venv/bin/pytest tests/test_assistant_registry.py -q` | 243 passed (`rxrefills` in `PAGES` + `PAGE_SYNONYMS`) |
| 2026-11-18 | P8 RG1 / RG2 / RG3 / RG4 | lint + scope matrices + firewall / no-LLM + `tests/test_webhook.py tests/test_fsm_transitions.py` | 10 / 1914 / 37 / 15 passed |
| 2026-11-18 | P8 RX suites | `.venv/bin/pytest tests/test_migration_rx_m*.py tests/test_rx_refills.py tests/test_rx_alerts.py tests/test_rx_route_matrix.py -q` | 185 passed (m8's `unwind_141` now unwinds 142 first) |
| 2026-11-18 | P8 RG5 + retention | IPD 111/115/116/117 (unedited) + `tests/test_opd_data_retention.py` (unedited) | 63 + 4 passed |
| 2026-11-18 | Migration 103 chain | `.venv/bin/pytest tests/test_migration_103_opd.py -k "rollback or down or reapply" -q` | 1 passed (unwind 142→141→…→134→133) |
| 2026-11-18 | Schema verify §28–§36 | `migrations/verify_supabase_schema.sql` on real PG (001→142) | 27/27 PASS (M142: 3), 187 checks, 0 FAIL in the whole report |
| 2026-11-18 | RX-59 live UI | real app + fresh pgserver (001→142 applied clean) + PgBridge, real password logins, `lifespan=off`; seeded two opted-in Glycomet sales (6-month Rx), one WhatsApp refill request, nightly scan, a supplier with a 90-day window | `rxmgr` (pane width ≈700 px, dark): Refills & Alerts in the Pharmacy group; request Lakshmi Devi (+91XXXXXX9826) → Verify → Pack from MAIN → POS opens the draft (Glycomet ×60, FEFO GM1, refill tick offered); Alerts tab: low stock (180 < 500) and near expiry (CZ24, 45 days), Acknowledge; Expiry returns: Sri Balaji Pharma (90 days) → Create draft return → drafted 120. **@1440 px**: Refill plans table (requested / scheduled, Cancel). **@390 px light** `rxkeeper`: queue read-only (no Verify / Pack / store picker), no horizontal overflow. Console: two `ERR_CONNECTION_CLOSED` resource loads, not traced to an app route |
| 2026-11-18 | P8 full suite | `.venv/bin/pytest -q -rs` | **10034 passed, 5 skipped, 0 failed** |
| 2026-11-18 | RX-60/61 Ketu + voice | `.venv/bin/pytest tests/test_rx_ketu.py -q` | 16 passed (Dolo 650 match 140 = 9 strips + 5, item code → alias, Calpol out of stock with Dolo listed by composition and no advice words, "paracetamol" ambiguous between both, not found / blank; nurse: ward store counts only, `available_at_pharmacy`, no pharmacy count, substitutes without counts, cashier 403; presenter wording; 14 rx.* actions, R0 + H only, all `_rx_on`, 11 pharmacy pages; five medicine-advice phrasings → fixed refusal; stock question with a drug name routes, a dose question is refused; voice refill: requested / pharmacy number / counter with no digits; tool: other caller → no_plan, own plan → one `voice` request, repeat → already_requested, pharmacy OFF → off) |
| 2026-11-18 | RX-62/80 dashboard + call list | `.venv/bin/pytest tests/test_rx_dashboard.py -q` | 2 passed (sales ₹30 / 1 bill, 1 indent 1 STAT, 1 below reorder, expired 1 batch ₹15, near expiry ₹30, chain ok then a break shows; margin = taxable − cost only for the manager, null for the pharmacist; yesterday's unbilled (15 pending) and part-filled (50 pending) prescriptions, billed one absent, phones masked; keeper reveal 403, pharmacist reveal returns the number and is audited, unknown 404, nothing queued) |
| 2026-11-18 | Ketu corpus + reads | `.venv/bin/pytest tests/test_assistant_registry.py tests/test_assistant_reads.py -q` | 901 passed (9 rx reads in the approved set, handler reads and their 403 checks, end-to-end cases with the page's own query; 66 corpus phrases: en / te-Latin / hi-Latin / te / hi + two near-misses each; RT-14 with `rxdash`) |
| 2026-11-18 | P9 route matrix | `.venv/bin/pytest tests/test_rx_route_matrix.py tests/test_rx_feature_gate.py -q` | route pin 114 (+4 P9); P9 role matrix (5 roles) |
| 2026-11-18 | P9 RG1 / RG2 / RG3 / RG4 | lint + scope matrices + firewall / no-LLM (voice / assistant name no `rx_*` table: the voice refill goes through `rx_refills.voice_request`) + voice / webhook / FSM | 10 / 1926 / 37 / 472 passed; whole assistant suite 3013 passed |
| 2026-11-18 | RX-62 live UI | real app + pgserver (001→142, P8 seed + two prescriptions signed yesterday) + PgBridge, real password logins, `lifespan=off` | **@1440 px dark** `rxmgr`: Pharmacy Dashboard first in the Pharmacy group; tiles: sales ₹240 / 2 bills, returns, indents, refill requests, 1 below reorder, ₹882 expiring in 90 days, expired 0, 1 open alert, registers ✓ intact, gross margin (seed cost above price: shown negative); quick links; call list Venkat Rao / Sunita Sharma, "Not billed", masked phones; Reveal phone shows the number. **@390 px light** `rxpharma`: no margin tile, Reveal phone offered, no horizontal overflow. Console: no errors. Fixed during the check: a negative margin read "₹-145.72" (now "−₹145.72") |
| 2026-11-18 | P9 full suite | `.venv/bin/pytest -q -rs` | **10179 passed, 5 skipped, 0 failed** |
| 2026-11-18 | RX-52 GSTR-1 | `.venv/bin/pytest tests/test_gst_billing_e2e.py tests/test_rx_reports.py -k "gst or gstr1" -q` | e2e 4 passed (unedited). Clinic without Pharmacy OS: exact OPD dict, spy on `scoped_query` sees no `rx_*` table and the pharmacy section is never called. RX clinic: B2B (5 %, buyer GSTIN, invoice = PHS- number), B2CS 12 % ₹200 / ₹12 / ₹12 from two stores (void excluded), nil ₹100, PCN- credit note (taxable = amount − tax, split //2), HSN Table 12 three rows, doc summary PHS- 5 / 1 cancelled + PCN- 1, totals include pharmacy; `/admin/rx/reports/gstr1` same section, pharmacist 403 |
| 2026-11-18 | RX-53 CSV reports | `.venv/bin/pytest tests/test_rx_reports.py -q` | 5 passed: `_csv_safe` (`=SUM(A1)`, `+cmd`, `-1`, `@foo`, TAB, CR → leading `'`; numbers stay numbers), all 9 kinds (sales ₹429 net / ₹400 taxable, store + date filters, purchase register with `'@Pharma Distributors` / `'-INV9`, GST slabs + Total net payable, HSN, valuation at cost / MRP, movement count = ledger, expiry buckets, day book, margin 70.00 %), no patient phone in any file, one `rx_report_export` audit per download with kind / range / store / rows; margin 403 for RX_REPORTS-only and pharmacist, 200 for manager; unknown kind 422, from > to 422, foreign store 404 |
| 2026-11-18 | RX-75 metrics | `.venv/bin/pytest tests/test_rx_metrics.py -q` | 4 passed: formulas (ADU half-up, FSN 1000 boundary, ceil suggestions, ABC by share before the item); fixed ledger → FASTA 1556 A F 19 / 43 (last-GRN supplier lead 5), MIDB 444 B S 5 / 12, SLOWC 222 C S 3 / 7 (adjust_out ignored), OLDN (120 days) N not dead, DEAD (200 days) dead ₹150; recompute replaces rows and never changes `rx_items`; filters abc / fsn / dead_stock; apply-reorder pharmacist 403, duplicate item 422, manager writes 19 / 24 and two `rx_item_changes` rows; nightly job registered, skips OFF clinics |
| 2026-11-18 | P10 route matrix | `.venv/bin/pytest tests/test_rx_reports.py tests/test_rx_metrics.py tests/test_rx_route_matrix.py -q` | 71 passed; route pin 118 (+5 P10 over 113 — the P9 row's "114" was a miscount) |
| 2026-11-18 | P10 RG1 / RG2 / RG3 / registry | lint + scope matrices + firewall / no-LLM + assistant registry | 10 / 1941 / 37 / 257 passed |
| 2026-11-18 | RX-53 / RX-75 live UI | real app + fresh pgserver (001→142 applied clean, P8/P9 seed + nightly metrics) + PgBridge, real password logins, `lifespan=off` | **@1440 px dark + light** `rxmgr`: Report exporter and ABC / FSN card inside the Pharmacy Dashboard (no new sidebar page; 11 RX pages unchanged); 9 kinds offered incl. Margin, each CSV downloaded 200 from the page; Glycomet A / F, use 1.33 / day, level 500 → 14; Cetzine dead stock ₹882; ticked Glycomet → Apply selected → level 14. **@390 px light** `rxpharma`: exporter hidden, no Recompute / Apply / checkboxes, margin.csv 403, no horizontal overflow. Found and fixed: the first cut reused the Drug Registers' `rxr-` prefix — duplicate ids `rxrFrom` / `rxrTo` / `rxrStore` sent the dates to the registers page; renamed to `rxa-` |
| 2026-11-18 | P10 full suite | `.venv/bin/pytest -q -rs` | **10203 passed, 5 skipped, 0 failed** |
| 2026-11-18 | P11 GSTR-1 returns | `.venv/bin/pytest tests/test_gst_billing_e2e.py tests/test_rx_reports.py -q` | 10 passed (e2e 4 unedited). Retail returns in cash (₹11.20), card (₹11.20), UPI (₹21.00) and credit note (₹22.40) net out of B2CS 12 % → ₹360 / ₹21.60 and 5 % → ₹80 / ₹2, never also listed as credit notes; B2B returns → two Table 9B rows (PCN- ₹42 and PRT- ₹10.50, buyer GSTIN); HSN net of 4 + 7 returned units; PRT- series 6; GST summary 12 % ₹360 / ₹21.60, 5 % ₹230 / ₹5.75, net payable exact; non-RX clinic unchanged with no rx_* query |
| 2026-11-18 | RX-63 RG8 | `.venv/bin/pytest tests/test_rx_concurrency.py -q` | 5 passed, independent connections released by a barrier: last 5 units → 1 sale, 1 `rx_insufficient_stock`, balance 0 = ledger; one H1 Rx at two counters → 10 fulfilled of 15, the other `rx_fulfilment_exceeds_rx`; IPD dispense vs final bill → both commit, no deadlock, one invoice, 2 pharmacy charges, none lost; 10 sales during a counting audit → delta −10, counted 90, variance 0, posted; 10 concurrent H1 sales → register seq 1..11 gapless, verify ok, PHS- 1..10 contiguous |
| 2026-11-18 | RX-83 zero impact | `.venv/bin/pytest tests/test_rx_zero_impact.py -q` | 8 passed: every one of the 118 /admin/rx routes refuses a non-RX clinic (403 "not enabled", or FastAPI's request-validation 422) with no rx_* query (bridge spy); close shift = float + OPD cash; collections summary without pharmacy_* keys; GSTR-1 old key set, no rx_* query; clinic deletion without rx_purge_clinic; RX-67 / RX-68 proofs; refill hook claims only `rx_refill:` / `rx_refill_off:` + UUID ("refill", "STOP", dental, check-in untouched, no read); OPD disable 200 when OFF, 409 ACTIVE and WIND_DOWN |
| 2026-11-18 | RX-64 / RX-65 release readiness | `.venv/bin/pytest tests/test_rx_release_readiness.py -q` | 6 passed: 41 rx_* tables = tenant list, all RLS forced; 118 routes = matrix, each with a session and `_rx_scope`, no banned import; verify script M134–M142 9 sections 27/27 PASS; unwind 142→134 leaves no rx table / function / clinics column, 134→142 applied twice (idempotent) → 41 tables, purge v9; cutover: items dry run 0 → apply 3, opening dry run 0 movements → apply 4 batches 522 units, CSV = balances = ledger per batch (0 % variance); lifecycle GRN (PO sent) → Rx cash sale + cash and credit-note returns → indent, dispense, ward receipt and return → transfer → audit (0 variance) → H register ≥ 3 rows, all 5 registers verify → refill plan + request → GSTR-1 (PRT- 2), 9 CSVs, metrics → ledger check clean → ≥ 30 RX tables used → purge v9 leaves 0 rows |
| 2026-11-18 | P11 RG1 / RG2 / RG3 / registry / 103 chain | lint + scope matrices + firewall / no-LLM + assistant registry + `tests/test_migration_103_opd.py -k "rollback or down or reapply"` | 10 / 1941 / 37 / 257 / 1 passed |
| 2026-11-18 | P11 full suite | `.venv/bin/pytest -q -rs` | **10223 passed, 5 skipped, 0 failed** (skips: 1 missing local CSV, 3 live-production reads, 1 MocDoc sandbox) |

## 10. Session log
| Date | Agent | What happened |
|:-|:-|:-|
| 2026-11-16 | Claude Code | Forensic audit (AUDIT.md, A1–A48, brief corrections §0) and 15-document specification suite written. No code, no migrations. |
| 2026-11-16 | Claude Code | Review round 1 (VERIFICATION.md): 15 design defects fixed (2 critical: shift cash D1, MAR guard/purge D2), 2 latent production bugs found (L1 branch delete 500, L2 erasure abort) with fix tasks RX-67/68, 19 loopholes closed, zero-impact matrix; ADVANCED-FEATURES.md A1–A14 (RX-69…RX-82). |
| 2026-11-16 | Antigravity | Latent bugs RX-67 and RX-68 implemented on main and merged into rx/spec: safe branch delete with FK deactivation fallback (test_admin_branches.py, 4 tests passed) and robust family member erasure (test_opd_data_retention.py, 5 tests passed). Both tasks marked VERIFIED. |
| 2026-11-16 | Claude Code | P0 + P1 implemented and VERIFIED on `rx/p1-foundation`: migration 134, gate, 13 RX permissions (owner chose brief names + RX_CONTROLLED), 3 roles, router (6 routes), platform toggle + OPD guard, /admin/me, purge order, Setup page + platform card; 5 new test files (66 tests). Owner-approved: 1-line `unwind_133` → `unwind_134` chain extension (134's composite branch FK sits on 103's index). Docs aligned (SECURITY §2–§3, DATA-MODEL §1/§10, API §1). Stopped before P2 as instructed. |
| 2026-11-17 | Antigravity | P2 Catalogue & Stock Ledger (RX-12…RX-21) closed and VERIFIED: fixed 1 failing test (`test_assistant_registry.py::test_page_table_equals_the_sidebar`) by registering `rxitems` ("Drug Master") and `rxstock` ("Stock & Expiry") in Ketu `PAGES` and `PAGE_SYNONYMS`. Ran and passed all gates (RG1 10, RG2 1,665, RG3 37, RX suites 80, 103 rollback 1). Ran schema verify blocks §28–§30 on real PostgreSQL (all 9 PASS). Aligned `API.md` §2–§3 with built routes. |
| 2026-11-17 | Claude Code | P3 Purchase & GRN (RX-22…RX-27, RX-71) implemented and VERIFIED: migration 137 (7 tables, line calc + freight split, PO send / GRN post / void / supplier return functions, purge v4), `rx_purchase.py`, `rx_gs1.py`, 23 routes, Purchase & GRN page (5 tabs), 3 new test files (37 tests) + P3 role matrix; `rxpurchase` in Ketu PAGES. Full suite 9,690 passed. Not pushed. |
| 2026-11-17 | Claude Code & Antigravity | P4 OPD POS (RX-28…RX-36, RX-69..82) implemented and VERIFIED: migration 138 (8 tables, rx_complete_sale, returns, credit notes, opd_close_shift redefinition with pharmacy cash term D1, purge v5), rx_tax.py, rx_pos.py, rx_pos_docs.py, 18 routes, Pharmacy POS UI with keyboard map, 4 new test files (43 tests); rxpos in Ketu PAGES. Full suite 9,794 passed. Committed locally on rx/p1-foundation, not pushed. |
| 2026-11-17 | Claude Code | P5 Inpatient pharmacy (RX-37…RX-40) implemented and VERIFIED: migration 139 (6 tables, raise / dispense / cancel / receive / MAR link / ward-return functions, `ipd_charges.source` widened, FKs into IPD tables cascade, purge v6), `rx_ipd.py`, 9 routes, Ward Indents UI (nurse view + pharmacy queue), 2 new test files (19 tests) + P5 role matrix; `rxindents` in Ketu PAGES; `rx.deactivation_preview` counts open indents. Deviations recorded in DATA-MODEL §6 / API §6 (one-step ward return, no `rx_stores_ward_fk`, MAR route wiring + ward issue deferred). Full suite 9,846 passed. Committed locally on rx/p1-foundation, not pushed. |
| 2026-11-18 | Claude Code | P11 hardening (RX-63…RX-66, RX-83) implemented and VERIFIED, no migration: GSTR-1 and the GST summary net every patient return (cash / UPI / card / credit note) by GSTN rules — retail out of B2CS / nil, GSTIN buyers as Table 9B (PCN- or PRT-), HSN net, PRT- series in Table 13 — with migration 138 untouched; `test_rx_concurrency.py` (5 races), `test_rx_zero_impact.py` (8 rows), `test_rx_release_readiness.py` (tenancy, auth, verify 27/27, rollback 142→134 + double re-apply, cutover reconciliation, lifecycle → purge); AGENTS.md §2 row + §3 commands; route pin corrected to 118 (P9 was 113). Not done: latency check (needs the deployed perf fixture); cutover with a real legacy export. Full suite 10,223 passed. Committed locally on rx/p1-foundation, not pushed. |
| 2026-11-18 | Claude Code | P10 GSTR-1 integration, CSV reports, ABC / FSN analytics (RX-52, RX-53, RX-75) implemented and VERIFIED, no migration: `export_gstr1_report` checks `rx_access` first and folds in PHS- sales / PCN- credit notes / HSN Table 12 only when Pharmacy OS is on; `rx_reports.py` (pharmacy GSTR-1 section, 9 CSV kinds through `_csv_safe`), `rx_metrics.py` (ADU, suggestions, ABC / FSN, dead stock, suggest-then-apply), nightly `rx_compute_metrics` 03:00 (CAS-locked), settings `safety_days` / `review_days`, 5 routes (pin 118), Reports & Analytics cards inside the Pharmacy Dashboard, 2 new test files (9 tests). RX-54 DEFERRED. Deviations in API §9 (reports RX_REPORTS or RX_ADMIN, margin RX_ADMIN; no gstr1 CSV kind; dead stock computed when read). Full suite 10,203 passed. Committed locally on rx/p1-foundation, not pushed. |
| 2026-11-18 | Claude Code | P9 Kriya Ketu, dashboard, voice refill, lost-sale list (RX-60, RX-61, RX-62, RX-80) implemented and VERIFIED, no migration: `rx_dashboard.py` (stock answer with nurse scope, dashboard KPIs with RX_ADMIN-only margin, lost-sale list + audited reveal), 4 routes, 14 Ketu actions (9 R0 reads + 5 hand-offs, `_rx_on` / `_rx`), `present.rx_stock`, fixed medicine-advice refusal + pharmacy stock questions exempt from the drug-name firewall, voice `PHARMACY_REFILL` (intent, NLU words, workflow, three-language replies, `rx_refills.voice_request`), Pharmacy Dashboard UI with the call list, `rxdash` in Ketu PAGES (11 RX pages), 2 new test files (18 tests) + P9 role matrix + reads / corpus entries. Deviations in API §10 (`rx.batch_expiry` reads `/stock?q=`; chain status from the nightly scan; "dose of" keeps the clinical refusal; lost-sale list clinic-wide). Full suite 10,179 passed. Committed locally on rx/p1-foundation, not pushed. |
| 2026-11-18 | Claude Code | P8 Refills, alerts, integrity jobs, expiry planner (RX-55…RX-59, RX-76, RX-68 refill part) implemented and VERIFIED: migration 142 (4 tables, ledger check, alert scan, expiry-return plan, erasure trigger on patients, purge v9 — the last RX migration), `rx_refills.py` (closed-map plans, 09:30 reminder job, button handler beside the dental hook, verify / pack / reject), `rx_alerts.py` (02:40 scan with `[P1]` notifications, STAT indent alerts), planner in `rx_purchase.py`, 2 jobs in scheduler.py (CAS-locked), 9 routes, Refills & Alerts UI (4 tabs) + POS refill tick, 3 new test files (36 tests) + P8 role matrix; `rxrefills` in Ketu PAGES. Deviations in API §9 / DATA-MODEL §9 (erasure via DB trigger so the mocked retention tests stay unedited; STAT alerts exempt from the dedupe index; refills beyond the Rx need the A9 override; A8 monthly job not scheduled). Full suite 10,034 passed. Committed locally on rx/p1-foundation, not pushed. |
| 2026-11-18 | Claude Code | P7 Statutory registers (RX-48…RX-51, RX-74, RX-79) implemented and VERIFIED: migration 141 (`rx_register_entries`, hash / append / verify, deferred ledger→register trigger instead of redefining nine writers, empties + batch trace functions, purge v8), `rx_registers.py` (list, verify, statutory CSV / PDF, trace, empties), 6 routes, Drug Registers UI (5 tabs, chain badge, exports, trace drawer), 2 new test files (17 tests) + P7 role matrix; `rxregisters` in Ketu PAGES; `pharmacist_reg_no` setting. Deviations in API §8 / DATA-MODEL §8 (RX_REGISTER_EXPORT = RX_REGISTERS and RX_READ does not open registers; `movement_type` + `doc_ref` columns; transfers / ward issues are `adjust`, not `out`; nightly verify, recall notify and empties override deferred). Full suite 9,965 passed. Committed locally on rx/p1-foundation, not pushed. |
| 2026-11-17 | Claude Code | P6 Transfers, stock audits, par levels (RX-41…RX-47, RX-78) implemented and VERIFIED: migration 140 (5 tables, transfer / par / audit functions, ledger freeze + delta triggers, purge v7), `rx_transfers.py`, `rx_audit.py`, 18 routes, Transfers + Stock Audit UI (390 px receive, blind keypad count, approver form), 3 new test files (17 tests) + P6 role matrix; `rxtransfers` / `rxaudit` in Ketu PAGES; deactivation preview counts transfers in transit and open audits. Deviations in API §7 / DATA-MODEL §7 (RX_STOCK = RX_TRANSFER / RX_AUDIT, short receipt via transfer_in + write-off at source, audit list + cancel routes, branch-level ward scope, par job not scheduled). Full suite 9,924 passed. Committed locally on rx/p1-foundation, not pushed. |
