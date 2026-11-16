# Kriya Pharmacy OS — Forensic Code Audit (file:line evidence)

> Part of the Pharmacy OS set: start at [status.md](status.md). **Binding.** Every design claim in this folder that
> touches existing code is backed by a line below. Audited at `main` @ `e5f99b5` (2026-11-16). Before editing any file
> named here, re-locate the symbol (lines move); if the code no longer matches the "Finding", stop and update this file
> and the affected design doc in the same commit (AGENT-PROMPT §3, RX-01).

Legend: **F** = finding (what the code does) · **→** = consequence for Pharmacy OS · IDs `A1…` are cited from other docs.

## 0. Corrections to the brief
| Brief said | Code says | Evidence |
|:-|:-|:-|
| `ipd_orders` at 111 lines 415–450 | `ipd_orders` is **111:307–337**; 415–450 is `ipd_discharge_summaries` (111:416) | `migrations/111_ipd_os_core.sql:307`, `:416` |
| table `ipd_mar` at 111 lines 470–510 | There is no `ipd_mar` table. MAR is **`ipd_med_administrations` 111:342–365**; `ipd_mar_*` is only the constraint prefix. 470–510 are guard functions | `migrations/111_ipd_os_core.sql:342`, `:359` |
| `opd_prescriptions` 325–370 | Confirmed: table 325–366, partial uniques 367–369 | `migrations/103_opd_os_core.sql:325` |
| `opd_prescription_items` 372–395 | Confirmed: 372–395 | `migrations/103_opd_os_core.sql:372` |
| "loaders map (~line 17416)" | `const loaders = {…}` is at **admin/index.html:17909** (single line), dispatched at 17910 | `admin/index.html:17909` |
| 120 "drug masters if present" | **No drug master exists anywhere.** 120 holds `billable_services` (no drug category) and `icd10_codes` | `migrations/120_clinical_coding_and_masters.sql:26`, `:288` |
| `TENANT_OWNED_TABLES` "41 DX tables" | Confirmed: 135 names total — 41 `dx_*`, 19 `ipd_*`, 10 `opd_*` (counted by parsing the frozenset) | `app/tenancy.py:43–126` |

## 1. OPD prescriptions (migration 103)
| ID | Finding | Evidence | → Pharmacy OS |
|:-|:-|:-|:-|
| A1 | Prescription header is versioned (`version` 1–99, `supersedes_id`), status `draft`/`signed`/`superseded`; one draft and one signed per appointment (partial uniques) | `103:333–335`, `103:367–368` | POS loads **only `status='signed'`**; a superseded Rx is never fulfilled (W2 step 3) |
| A2 | A signed Rx must carry `signer_snapshot ? 'registration_number'`, `letterhead_snapshot`, `patient_snapshot` (CHECK `opd_prescriptions_signed_complete`) | `103:355–358` | The Schedule H/H1/X register copies the prescriber registration number **from `signer_snapshot`**, never from the live `doctors` row (ADR-RX-05) |
| A3 | Signed rows are locked by `opd_guard_signed_record`; only delivery columns + supersede move | `103:549–585`, trigger `103:764` | Pharmacy never writes to `opd_prescriptions`; fulfilment state lives in `rx_rx_fulfilments` (DATA-MODEL §6) |
| A4 | Items: `drug_name` **free text** 2–200 chars; `formulation` enum (tablet…other); `strength` free text ≤60; `dosage` free text; `route` enum; `frequency` free text ≤40 (OD/BD/TDS…); `timing` enum; `duration_days` 1–365 or NULL (=SOS/until review); `instructions` | `103:372–395` (drug_name 377, formulation 378–379, strength 380, dosage 381, route 382–383, frequency 384, timing 385–386, duration 387, instructions 388) | No FK to any item master → POS must **map** free text to `rx_items` deterministically (trigram + alias table) and the pharmacist confirms each line (ADR-RX-07). Quantity to dispense is computed from `frequency × duration_days` only for the closed frequency set; anything else = pharmacist enters quantity |
| A5 | Items are writable only while parent is draft (`opd_guard_rx_items`) | `103:587–599`, trigger `103:770` | A signed item id is a stable reference → `rx_sale_items.opd_prescription_item_id` composite FK is safe |
| A6 | `(clinic_id, id)` uniques on both tables | `103:353`, `103:390` | Composite FKs from `rx_*` are possible without new indexes |

## 2. OPD invoicing, receipts and shifts (103, 117, `opd_billing.py`)
| ID | Finding | Evidence | → Pharmacy OS |
|:-|:-|:-|:-|
| A7 | `opd_invoices` gapless number `INV-YYYY-NNNNN` from `opd_next_counter` + `opd_format_number`; one live invoice per appointment | `103:398–449`, `103:195`, `103:212`, unique `103:450` | Pharmacy sales do **not** use `opd_invoices` (it would collide with the one-per-visit rule and its tax-exclusive model). Own series `PHS-YYYY-NNNNN` from the same `opd_next_counter` with key `rx_sale` (ADR-RX-08) |
| A8 | `opd_invoice_items.item_type` ∈ consultation/nursing/diagnostic/procedure/other — no `pharmacy` | `103:464`; Python mirror `opd_billing.py:1590–1591` | Confirms OPD billing has no medicine line type; we do not widen it |
| A9 | GST is **tax-exclusive**: `line tax = round_half_up(line_total × bps / 10000)`, CGST = tax/2, SGST = rest, IGST 0; GSTIN gate (no GSTIN → rate forced 0) | `117:14–23`, `opd_gst_amount 117:42–45`, `opd_clinic_gstin 117:47–50`, trigger `117:96–119` | Retail medicine prices are **MRP-inclusive**; we need a back-calculation (`rx_gst_split_inclusive`) that reuses the same half-up integer rule and the same GSTIN gate (ADR-RX-08) |
| A10 | Allowed slabs CHECK `tax_rate_bps IN (0,500,1200,1800,2800,4000)`; `hsn_sac ~ '^[0-9]{4,8}$'` | `117:52–56`, `117:88–91` | `rx_items.tax_rate_bps` uses the identical CHECK so every rate flows through to `ipd_charges` unchanged |
| A11 | Python `gst_amount` / `calculate_line_gst` mirror SQL exactly | `app/services/gst_billing.py:40–50` | Reuse in POS preview; property test asserts Python == SQL (TEST-PLAN §4) |
| A12 | Credit notes `CRN-YYYY-NNNNN`, append-only, insert only inside `opd_issue_credit_note` (GUC gate) | `117:236–290`, function `117:292` | Pharmacy credit notes need their own table `rx_credit_notes` (they credit `rx_sales`, not `opd_invoices`) with the **same** GUC-gate pattern and its own series `PCN-` |
| A13 | GSTR-1 export reads only `opd_invoices` | `gst_billing.py:174` | Pharmacy B2C sales are absent from GSTR-1 until RX-52 adds `rx_sales` to the export (GAP G14) |
| A14 | Cashier shifts: one open per cashier; receipts reference shift; `close_shift` and `get_collections_summary` total `opd_receipts` only | `103:483–505`, `103:507–547`, `opd_billing.py:885`, `:984`, `:1052` | POS payments carry `shift_id` → `opd_cashier_shifts`. **Correction (VERIFICATION D1/A49):** expected cash is computed in SQL `opd_close_shift` (116:760), so migration 138 redefines that function to add pharmacy cash; non-RX shifts get an identical result |
| A15 | Server-side pricing `_validate_and_price_items`; non-admin cannot override order prices | `opd_billing.py:1548–1600` | POS repeats the rule: price = batch MRP per unit (server), discount only within `rx_settings.max_discount_bps` unless `RX_ADMIN` |

## 3. IPD orders, MAR and charges (111, 115, 116, 117, `ipd.py`)
| ID | Finding | Evidence | → Pharmacy OS |
|:-|:-|:-|:-|
| A16 | `ipd_orders.order_type='medication'` requires `drug_name, dose, route, frequency` (CHECK); `drug_name` free text ≤120; no quantity, no duration, no item FK | `111:307–337`, CHECK `111:331–332` | Indent quantity is entered by the nurse and verified by the pharmacist; `rx_indent_lines.rx_item_id` maps the free text (W4) |
| A17 | Order content immutable; only `active → completed/discontinued` (`ipd_guard_order`) | `111:583–602`, trigger `111:690` | Indent references a **stable** order; an indent line is refused once the order leaves `active` (trigger `rx_check_indent_order`) |
| A18 | Only a doctor with `IPD_CLINICAL` may end a medication order | `app/routers/ipd.py:684–699` | Pharmacy never ends or alters orders (ADR-RX-06) |
| A19 | MAR = `ipd_med_administrations`; insert checks order is active medication of the same admission and the stay holds a bed; rows locked except entered-in-error | `111:342–365`, `ipd_check_mar_insert 111:559–571`, guard `111:694–696` | **Correction (VERIFICATION D2/A52):** no column is added to MAR (its guard forbids the purge UPDATE v1 needed). Batch traceability lives in RX-owned `rx_mar_links` with `ON DELETE CASCADE` from MAR |
| A20 | `ipd_charges.category` already includes `'pharmacy'`; `source` ∈ manual/bed_day/order; `source_ref` partial unique per admission | `111:380–415` (category 384–385, source 391, unique 411–412) | Dispense posts `category='pharmacy'`; we **widen** `source` CHECK to add `'pharmacy'` (additive) and use `source_ref='rx:dl:<dispense_line_id>'` → idempotent by construction |
| A21 | Charge amounts immutable; `posted → void` only while unbilled; billed charge locked (`ipd_guard_charge`) | `111:604–630` | Returns before final bill = void + re-issue for kept quantity; after final bill = credit note on the IPD `opd_invoices` row via `opd_issue_credit_note` (W5b) |
| A22 | 115 mirrors investigation/procedure orders into `opd_orders` and proposes a charge; **medication orders are skipped** | `115:516–549` (skip at 519), triggers `115:586–592` | No existing path charges medicines automatically — pharmacy is additive, no double-charge risk with 115 |
| A23 | `ipd_charges.tax_rate_bps` / `tax_paise` (117), room-rent trigger only touches `category='bed'` | `117:88–94`, `117:371–392` | Inpatient medicines are part of composite healthcare supply (exempt) → dispense posts `tax_rate_bps=0` with the item's HSN kept for audit (ADR-RX-08 §IPD) |
| A24 | IPD service `add_order`, `record_administration`, `add_charge` insert through `_insert` with idempotency replay | `app/services/ipd.py:494–510`, `:537`, `:563`, `:619` | Pharmacy dispense goes through one SQL function (`rx_dispense_indent`) — not `add_charge` — so stock movement and charge commit atomically |
| A25 | `ipd_purge_clinic` deletes IPD children then calls `opd_purge_clinic` | `111:1303–1330` | `rx_purge_clinic` must run **before** `ipd_purge_clinic` (rx rows FK into ipd/opd) — platform purge order updated (RX-08) |
| A26 | `ipd_bill_lines` groups posted charges for the final bill | `116:326` | Pharmacy lines appear on the final bill grouped by description; description = "Brand strength (batch)" keeps lines distinct per batch |

## 4. Masters (migration 120)
| ID | Finding | Evidence | → Pharmacy OS |
|:-|:-|:-|:-|
| A27 | `billable_services.category` has no medicine/drug value; HSN default `999312` (SAC services) | `120:26–48` (category 33–35, hsn 38, tax 40) | Medicines are goods (HSN chapter 30), not services → own `rx_items` master; we do not add drugs to `billable_services` |
| A28 | `icd10_codes` is a shared, clinic-less reference table | `120:288` | `rx_generics` follows the same pattern only if shared; we keep generics **per clinic** (formularies differ) — see DATA-MODEL §2 |
| A29 | `opd_guard_billable_service`: a code is history, never changes | `120:52–58` | Same rule for `rx_items.item_code` |

## 5. Tenancy, database helpers, feature gates, permissions
| ID | Finding | Evidence | → Pharmacy OS |
|:-|:-|:-|:-|
| A30 | `TENANT_OWNED_TABLES` frozenset; comment requires `clinic_id` on each | `app/tenancy.py:43–126` | Every `rx_*` table registered in the migration's task (DATA-MODEL §12 lists 36) |
| A31 | `is_valid_clinic_scope` rejects `default`, `all`, `*` … | `app/tenancy.py:135–141` | `_rx_scope` calls it first |
| A32 | `sb()` runs builders in the executor; unscoped builder on a tenant table raises unless `_allow_unscoped` | `app/database.py:157`, `:206` | Lint DG1 catches any unscoped `rx_*` read |
| A33 | `scoped_query` fails closed | `app/database.py:293–301` | — |
| A34 | `OPT_IN_FEATURES` has opd/ipd/dx/kriya_ketu; owner-only toggles, never plan wildcard | `app/services/tenant.py:666–673` | Add `"pharmacy_enabled": "Pharmacy OS (Stores, Purchase, POS, IPD Dispensing, Registers)"` |
| A35 | `ipd_access` = ACTIVE / WIND_DOWN / None; requires OPD | `tenant.py:682–697` | `rx_access` copies the shape; requires `opd_enabled` (patients, MRN, shifts, counters) |
| A36 | `PHARMACY_OPERATOR` role exists with **empty** preset | `app/services/permissions.py:153`, `:201` | Pharmacy roles are new; `PHARMACY_OPERATOR` gains `RX_READ` + `RX_DISPENSE` by role-implied grant only while `rx_access` is ACTIVE (SECURITY §3) |
| A37 | `holds_dx_permission` pattern for implied grants | `permissions.py:395` | `holds_rx_permission` copies it |
| A38 | Platform purge order: dx → ipd (which purges opd) | `app/routers/platform.py:2587–2632` | Insert `rx_purge_clinic` first |

## 6. Admin UI (`admin/index.html`, 31 809 lines)
| ID | Finding | Evidence | → Pharmacy OS |
|:-|:-|:-|:-|
| A39 | Theme tokens on `:root` dark (`--accent:#3D8BFD` at 52) and light (`--accent:#1668D6` at 132), `--green/--red/--amber` families | `admin/index.html:40–60`, `:132` | UI.md uses only these tokens (expiry chips = `--red`/`--amber`/`--green`) |
| A40 | Collapsible nav groups `.nav-group` CSS | `admin/index.html:736–771` | New group `data-nav-group="rx"` "Pharmacy" |
| A41 | OPD group 3792, IPD group 3828, Diagnostics group 3858; DX links wrapped in `<!-- DX:<lane>:START/END -->` and hidden by `data-dx` until `applyDxGates()` | `admin/index.html:3792`, `:3828`, `:3858`, `:3875–3884` | Pharmacy links use `<!-- RX:<lane>:START/END -->` and `data-rx="nav PERM…"` with `applyRxGates()` |
| A42 | `go(page, el)` opens a page; `const loaders = {…}` dispatch; DX entries inlined between block comments | `admin/index.html:17877`, `:17909–17910` | RX loaders appended inside `/* RX:<lane>:START */ … /* RX:<lane>:END */` |
| A43 | `admin/admin.js` is dead (AGENTS.md §1.7) | `AGENTS.md:17` | Never edited |

## 7. AI, firewall, refills, Ketu, voice
| ID | Finding | Evidence | → Pharmacy OS |
|:-|:-|:-|:-|
| A44 | Clinical firewall is deterministic; `screen_message` before LLM, `validate_llm_output` strips dosage/medicine content | `app/services/clinical_firewall.py:410`, `:485` | Refill replies and Ketu stock answers never pass through an LLM; `tests/test_rx_no_llm.py` AST guard (ADR-RX-06) |
| A45 | Legacy `prescriptions` (mig 002) = patient-entered dose reminders by phone; job `prescription_reminders` every 5 min | `migrations/002_lab_reports.sql:21–34`, `app/services/scheduler.py:125`, `:313–320` | **Not reused and not touched.** Refill reminders are a separate daily job `rx_refill_reminders` over `rx_refill_plans` (W10) |
| A46 | Ketu registry risk tiers `R0…R3,H`; `PAGES` table must equal sidebar (RT-14) | `app/assistant/registry.py:39`, `:45–90` | New pages registered; stock lookup is R0 (KRIYA-KETU.md) |
| A47 | Ketu already maps "medicine/drug/injection" words to IPD charge category `pharmacy` and hands nursing medicine entry to the chart | `app/assistant/resolve.py:2062`, `:2290`; `registry.py:1238–1241` | Existing H hand-off stays; RX adds R0 stock reads only |
| A48 | Voice intents include `PHARMACY_INFORMATION` | `app/voice/intents.py:20`, `app/voice/dialog.py:31` | Voice answers pharmacy hours/location only; stock queries over voice are **staff-only** via Ketu, never to patients (KRIYA-KETU §3) |

## 8. What does not exist (confirmed absent)
`grep -rli "pharmacy\|formulary\|drug_master" migrations app` returns only 111/116 (charge category), the IPD router
category literal, the Ketu resolver, voice intents, the firewall comment and the empty role preset. There is **no**
item master, batch, stock, supplier, purchase, sale, indent, transfer or register table anywhere. Pharmacy OS is
greenfield on top of the OPD/IPD/GST foundations above.
