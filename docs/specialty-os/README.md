# Kriya Specialty OS — index and execution status

> Start with [GAP-ANALYSIS.md](GAP-ANALYSIS.md) (cross-plan matrix, shared blocks S1–S9, priorities), then the plan
> doc you are building: [EYE.md](EYE.md) · [DENTAL.md](DENTAL.md) · [DERMA.md](DERMA.md) · [IVF.md](IVF.md) ·
> [WOMEN-CHILD.md](WOMEN-CHILD.md) · [MULTISPECIALTY-AND-DIAGNOSTICS.md](MULTISPECIALTY-AND-DIAGNOSTICS.md) ·
> [OWNER-CONSOLE.md](OWNER-CONSOLE.md). Standalone pharmacy: [../pharmacy-os/RETAIL-PLAN.md](../pharmacy-os/RETAIL-PLAN.md).
> Continuous-care packs per specialty: [../continuous-care/PROTOCOLS.md](../continuous-care/PROTOCOLS.md) §6.
> Existing specialty implementation (catalogue, WhatsApp flow, care pathway, service lines): [../specialty_plan/README.md](../specialty_plan/README.md)
> and [../SPECIALTY_EXPANSION_MASTER_MEMORY.md](../SPECIALTY_EXPANSION_MASTER_MEMORY.md).
>
> Same states and rules as [../continuous-care/status.md](../continuous-care/status.md): never VERIFIED without the
> pasted gate summary (CC TEST-PLAN §1 gates CG1–CG7 apply to every SP task); update this file in the same commit.

## 1. Current pointer
| Field | Value |
|:-|:-|
| State | **VERIFIED** (2026-11-25, working tree, uncommitted) — migrations 151–155 + services + `/admin/sp` + panel + owner console |
| Next task | Owner decisions SP-O2 (ART register formats) and SP-O3 (wallet GST); deferred items in §3a |
| Migrations | 151 media + forms · 152 packages / instalments / estimates / imaging / camps · 153 eye + dental · 154 IVF · 155 ANC + immunisation + purge chain · 156 deliveries / newborns / growth / gov ids / NICU / optical stock / IOL→IPD (all with rollbacks) |
| Owner decisions | SP-O1 which plan to deepen first (default: **eye** — optical store + exam form sells fastest and links to diabetes care); SP-O2 legal confirmation of ART register formats before SP-IVF-05; SP-O3 wallet/membership GST treatment before SP-DER-07 |

## 2. Shared blocks
| ID | Task | Depends | Acceptance | State |
|:-|:-|:-|:-|:-|
| SP-S1-01 | Clinical media vault (consent, private storage, signed URLs, compare view) | CC P1 patterns | DPDP erasure covers media; never sent without staff share action | VERIFIED — `test_sp_media.py` (EXIF stripped, consent gate, share needs `allow_share` + opt-in, erasure removes rows + files) |
| SP-S2-01 | Specialty forms engine: code-defined versioned schemas, `opd_encounter_forms`, freeze on sign, PDF block | OPD OS | prescription PDF unchanged when no form rows (golden PDF test) | VERIFIED — `test_sp_forms.py` (golden sha256 of the committed renderer; DB freeze trigger) |
| SP-S3-01 | OPD packages + consumption + instalments | OPD billing | package sale = ordinary invoice; GSTR-1 unchanged for non-package clinics | VERIFIED — `test_sp_commerce.py`, `test_gst_billing_e2e.py` unedited |
| SP-S4-01 | Specialty consent templates over `clinical_consents` | 119 | templates only | VERIFIED — 7 templates en/hi/te (`sp.CONSENT_TEMPLATES`), used by IVF freeze test |
| SP-S5-01 | Estimates with WhatsApp accept | S3 | no money moves on accept | VERIFIED — buttons `spest:`; replay / stranger ignored; receipts unchanged |
| SP-S6-01 | Imaging links per encounter | 123 | additive | VERIFIED (API; https-only CHECK) |
| SP-S9-01 | Camp module | CC P6–P8 | verified outcomes per camp | VERIFIED (API + report; verified only by check-in / signed encounter) |

## 3. Per-plan tasks (detail in each plan doc §Tasks)
| Plan | Tasks | First milestone | State |
|:-|:-|:-|:-|
| eye | SP-EYE-01…08 | exam form + spectacle Rx PDF + optical store | VERIFIED 01–08: 02 optical stock (`sp_optical_items` + append-only ledger, job issues frame + lens; kept outside the pinned `rx_*` tables), 05 accepted estimate → IPD package (116) applied to the patient's own stay, 06 one-tap CC enrollment — `test_sp_optical_ipd_care.py` |
| dental | SP-DEN-01…09 | odontogram + estimates + instalments | VERIFIED 01–09 (04 = one-tap CC enrollment into `dental_recall` / `dental_ortho` / `dental_implant_review`) |
| derma | SP-DER-01…07 | session packages + before/after photos | VERIFIED 01–06: 03 procedure form + 04 PASI / SCORAD / MASI / SALT in the panel (Derma records page) and on the prescription PDF, 05 one-tap CC; 07 wallet deferred (SP-O3) |
| ivf | SP-IVF-01…09 | couple + cycle + embryology witnessing | VERIFIED 01, 02, 03, 06 (trigger ack + one-tap `ivf_cycle` with trigger / transfer facts), 07; 04, 05, 08, 09 deferred (SP-O2) |
| womenchild | SP-WC-01…08 | ANC record + immunisation module | VERIFIED 01–08: 02 delivery → newborn family member (OPD registration path), 04 WHO z-scores (embedded WHO LMS tables, chart), 05 one-tap `anc` / `postnatal` / `child_immunisation` / `child_growth`, 06 birth registration due list, 07 RCH / U-WIN ids + CSVs, 08 NICU flowsheet — `test_sp_wc_depth.py` |
| multispecialty / diagnostics | MS-1…8, DXG-1…4 | delivered by CC, RI, MI lanes | NOT_STARTED |
| owner console | OC-01…09 | drift test + modules API + pharmacy wizard | VERIFIED OC-01…OC-09 (`test_sp_owner_console.py`; OC-09 typed confirmation is console-side) |
| pharmacy (retail) | RXR-00…12 | plan registry + shift routes + owner wizard | VERIFIED except RXR-11 (CC-D5) — see RETAIL-PLAN §10 |

## 3a. Deferred (blocked on an owner decision — documented, not hidden)
| Item | Blocked on |
|:-|:-|
| SP-IVF-04 cryo renewal billing, SP-IVF-05 ART register in the official format, SP-IVF-08 cycle packages, SP-IVF-09 andrology templates | SP-O2 — legal confirmation of ART Act / Rules register formats |
| SP-DER-07 wallet / membership credit | SP-O3 — GST treatment of prepaid credit |

Notes on what was built (migration 156): WHO LMS tables come from WHO's published files (who.int, downloaded
2026-11-26; source URL + sha256 per file in `app/services/sp_growth_tables.py`; WHO weight-for-age stops at 10 y, head
circumference at 5 y). U-WIN / RCH exports are Kriya's documented columns for staff upload or entry — the government
bulk formats are not public and Kriya never automates a portal. Optical stock is an SP ledger, not an RX store kind,
so the pinned `rx_*` table and route counts are unchanged; spectacles are still billed at the counter.

## 4. Zero-impact rules for every SP task
1. Plan-scoped features follow the existing helpers (`specialty_enabled`, `dental_plans_enabled`): on for the matching
   plan unless overridden False; on for hybrids/enterprise **only** with an explicit owner override; never via the
   enterprise wildcard.
2. New tables only; no trigger on existing tables; existing PDFs, menus and bills are byte-identical for clinics
   without the feature (golden/snapshot tests).
3. `tests/test_specialty_plans.py`, `tests/test_dental_plans.py` and every existing suite pass **unedited**.
4. Every SP screen follows [docs/design/NEXT-LANES-UI-SPEC.md](../design/NEXT-LANES-UI-SPEC.md) (blueprints §7.9,
   icons §3.2) and passes UI-G1…G12 before VERIFIED.

## 6. Gate evidence (fresh cluster, migrations 001–156 from scratch, 2026-11-26)
| Gate | Command | Result |
|:-|:-|:-|
| G1 | `test_lint_unscoped_queries.py test_phase4_scoped_queries.py` | 10 passed |
| G2 | `test_admin_super_admin_scope_matrix.py test_phase2_route_adversarial_matrix.py` | 2445 passed |
| G3 | firewall + `*_no_llm.py` | 56 passed |
| G4 | `test_webhook.py test_fsm_transitions.py tests/voice/` | 472 passed; `git diff --stat` on them empty |
| G5 | UI contrast suites | 47 passed, 1 skipped (pre-existing) |
| G6 | assistant registry / reads / GST e2e | 1007 passed |
| G7 | `scripts/test_migration_runner.py tests/test_migration_*.py` | 482 passed |
| G8 | `test_ri_* test_mi_* test_cc_* test_rxr_* test_sp_* test_specialty_* test_dental_* test_plan_registry_drift.py` | 904 passed |
| G9 | `pytest -q -rs` full suite | **11506 passed, 0 failed, 6 skipped** — exactly the six pre-existing skips (live data ×3, CSV fixture, MocDoc credentials, owner-console contrast). The first G9 run had 1 failure in the new `test_migration_sp_chain.py` (it named which table the 156 rollback refuses on; in the full suite another table has rows first) — the assertion now checks "refuses while any new table has rows"; G7 re-run 482 passed |
| UI | headless Chrome, live server on its own fresh cluster (seeded multispecialty clinic, every pack) | 60 screenshots at 1440 + 390 px, dark + light (console dark only): Derma records, eye exam + optical stock, dental, IVF, ANC / growth chart / gov ids / births, shared tools, CC outcomes, Pharmacy setup, Refills, Purchase, owner segments + modules. No horizontal scroll; console clean except Chrome's cold-start `/favicon.ico` 404 (pre-existing). Fixed during the pass: eye steppers overlapping at 390 px, derma scores lost on visit switch. 1024 / 768 px + keyboard / screen-reader passes remain owner sign-off |

## 5. Log
| Date | Entry |
|:-|:-|
| 2026-11-26 | Lane SP completed: migration 156 (W&C 02/04/06–08, optical stock, IOL → IPD), Derma records page (PASI / SCORAD / MASI / SALT), one-tap Continuous Care from every specialty page; `/admin/sp` now 82 routes (pinned); gates in §6 |
| 2026-11-25 | Lane SP built: migrations 151–155, `/admin/sp` (65 routes, pinned), Specialty pages + owner console segments / modules, Ketu reads `sp.optical_ready` / `sp.instalments_due` / `sp.licences_due`; gates in §6 |
| 2026-11-24 | Gap analysis and build plans for eye, dental, derma, IVF, women & child, multispecialty/diagnostics, owner console and the standalone pharmacy plan written from a code audit at `c8c37ff`; no code changed |
