# Kriya Pharmacy OS — Forensic Design Verification (review round 1)

> Part of the Pharmacy OS set: start at [status.md](status.md). This is the adversarial review of the v1 specification
> (commit `a7f14b7`) against the live code at `main` @ `e5f99b5`, done 2026-11-16. Every finding names the code that
> proves it, the fix, and where the fix now lives. **Binding**: an implementing agent must not re-introduce a design
> marked "superseded" here. New evidence rows extend [AUDIT.md](AUDIT.md) as A49–A60.

## 1. Method
1. Every place the design **touches existing code** (tables, triggers, functions, routes, jobs, UI) was traced to the
   code that runs today, including callers of anything we write to or reference by FK.
2. Every **destructive or state-changing existing workflow** that could meet an RX row (deletes, purges, erasure,
   module toggles, admission/discharge transitions, final bill, shifts) was walked with RX data present and absent.
3. Every RX money/stock/register path was attacked as a dishonest or careless user would (loophole register §5).
4. Every lock taken by RX functions was ordered against existing lock-takers (deadlock analysis §6).
5. Fixes were applied to the design docs in the same commit as this file; §8 maps each fix to its doc.

## 2. New code evidence (extends AUDIT.md)
| ID | Finding | Evidence |
|:-|:-|:-|
| A49 | Shift expected cash is computed **in SQL** `opd_close_shift`, summing `opd_receipts` + `ipd_deposits` cash only; 116 redefined it to add deposits | `migrations/116_ipd_finance.sql:760–779` |
| A50 | `resolve_counter_shift`: super-admin has no drawer → `None` for UPI/card, 403 for cash; auto-opens a shift when `auto_open_shift` | `app/services/opd_billing.py:503–557` |
| A51 | `opd_next_counter` row-locks the **`clinics` row** (`UPDATE clinics SET opd_counters…`); keys carry the year (`'inv:'||year`, `'rct:'||year`) | `migrations/103_opd_os_core.sql:195–208`, `:657`, `:704` |
| A52 | `ipd_guard_clinical_record` rejects every UPDATE of a `recorded` MAR row except status/error columns, and has **no purge bypass for UPDATE** | `migrations/111_ipd_os_core.sql:536–556` |
| A53 | IPD MAR route is `POST /admin/ipd/admissions/{id}/administrations`; models use `extra="forbid"` and the body is dumped **straight into the insert** | `app/routers/ipd.py:37–38`, `:231`, `:701–706` |
| A54 | `delete_branch` checks only appointments, doctor_branches, integration_connectors, clinic_admins; any other FK to `branches` (today `ipd_wards`, `ipd_admissions`, `dx_sites`, `dx_accessions`) makes the DELETE raise → generic 500 | `app/routers/admin.py:8441–8500`; FKs `111:65`, `111:168`, `125:41`, `127:121` |
| A55 | DPDP erasure deletes `family_members`; on failure it falls back per member, checking references only in appointments/opd_encounters/opd_prescriptions/opd_invoices; an FK from any other table raises inside the loop → the remaining members are left unprocessed and an error is recorded | `app/services/data_retention.py:385–452` |
| A56 | Global WhatsApp STOP already exists: intent `opt_out` → `patients.opted_in = false`; `accepts_engagement()` is the gate for engagement sends | `app/services/conversation.py:1158`, `:4691–4702`; `app/services/consent.py:20–37` |
| A57 | Template quick-reply arrives as `message_type="button"` with our payload; dental reviews claim a payload **prefix** before the FSM — the established hook pattern | `app/routers/webhook.py:299–306`; `app/services/conversation.py:680–689` |
| A58 | Inbound reply `context.id` is **not** persisted or passed to the conversation layer | grep of `webhook.py`, `conversation.py`, `message_queue.py` (no `context` id handling) |
| A59 | Platform feature toggle: refuses disabling OPD while `ipd_state ≠ OFF`; `enabled=None` pops the flag and falls into the module's disable branch | `app/routers/platform.py:900–1010` (guard 906–910) |
| A60 | Owner purge runs only if OPD encounters/invoices or IPD admissions exist; DX purge first, then `ipd_purge_clinic` or `opd_purge_clinic` | `app/routers/platform.py:2587–2632` |
| A61 | Discharge checklist blocks completion while posted charges have `invoice_id IS NULL`; `ipd_generate_final_bill` returns an existing live bill (does not re-collect later charges) | `migrations/117_gst_billing_and_credit_notes.sql:572–600`, `:431–445` |
| A62 | Packages cover any charge whose category is in the package's category list (incl. `pharmacy`) within package days | `migrations/116_ipd_finance.sql:340–341` |

## 3. Defects found in the v1 design (all fixed in this commit)
| ID | Severity | Defect in v1 | What would have broken | Fix (now in the docs) |
|:-|:-|:-|:-|:-|
| D1 | **Critical** | Pharmacy cash went into `opd_cashier_shifts` but v1 said only "Python summary keys" | Every pharmacy shift close would show a false **shortage** equal to the pharmacy cash (A49) | Migration 138 redefines `opd_close_shift` adding `rx_sale_payments` cash (116 precedent). Non-RX clinics: the added sub-select returns 0 → byte-identical result. Rollback 138 restores the 116 body. RG5 adds `test_close_shift_unchanged_without_rx` |
| D2 | **Critical** | `ALTER TABLE ipd_med_administrations ADD rx_dispense_line_id` + purge that UPDATEs MAR | `rx_purge_clinic` UPDATE on MAR is rejected by `ipd_guard_clinical_record` (A52) → **owner purge/clinic deletion fails**; the IPD route would also insert the unknown key into the table (A53) | **Superseded.** No IPD table gets a new column. Batch traceability moves to RX-owned `rx_mar_links(mar_id, dispense_line_id)`; the MAR route pops `rx_dispense_line_id` from the body, inserts the MAR exactly as today, then writes the link (best-effort, validated) |
| D3 | High | Refill replies matched on WhatsApp `context.id` and a pharmacy-own STOP keyword | `context.id` is never available (A58) → no reply would ever match; an RX STOP handler would shadow the global opt-out (A56) | Template **quick-reply buttons** with payloads `rx_refill:<plan_id>` / `rx_refill_off:<plan_id>`, claimed by prefix at the A57 hook. Global STOP untouched: it sets `opted_in=false`, which the refill job already honours via `accepts_engagement` |
| D4 | High | `rx_sales` CHECK `paid_paise = net_paise` while refunds were negative payments | First refund on any sale → CHECK violation → returns impossible | `paid_paise` fixed at completion; refunds tracked in `refunded_paise` (≤ paid) and `credited_paise`; CHECK `refunded_paise + credited_paise ≤ net_paise` |
| D5 | High | `rx_sale_payments.shift_id NOT NULL` | super-admin / drawer-less logins cannot take UPI/card at all (A50) | `shift_id` nullable; CHECK `mode <> 'cash' OR shift_id IS NOT NULL`; shift resolved by the existing `resolve_counter_shift(require_drawer = has_cash)` |
| D6 | High | IPD routes required `ipd_access == 'ACTIVE'` | When the owner puts IPD into WIND_DOWN, still-admitted patients could not get medicines | Indent/dispense/ward-issue require `ipd_access(clinic) is not None` **and** the admission `admitted|discharge_pending` (the DB trigger already enforces the latter) |
| D7 | High | No platform guard on disabling OPD under a live pharmacy | Pharmacy silently disappears mid-shift (rx_eligible needs OPD) with open drafts | Platform: refuse `opd_enabled=false/None` while `rx_state ≠ 'OFF'` (same 409 as IPD, A59); `pharmacy_enabled=None` handled exactly like `false` |
| D8 | High | Platform purge would call `opd/ipd_purge_clinic` while RX rows still reference patients/orders | FK violation → clinic deletion 500 (A60) | Purge gate also counts `rx_sales` + `rx_stock_movements`; `rx_purge_clinic` runs first whenever `rx_state` column exists (DX-style `select *` probe) |
| D9 | Medium | AFTER UPDATE trigger on `ipd_orders` to cancel indent lines | Adds a trigger to an IPD table (perf + coupling) | **Superseded.** No trigger on IPD tables. Queue and dispense read the order's status (`rx_dispense_indent` re-checks `ipd_orders.status='active'` under lock); stale lines show as "order stopped" and are closed by the pharmacist |
| D10 | Medium | Dispense did not serialise with `ipd_generate_final_bill` | A charge committed during bill generation is missed by the bill (A61 checklist then blocks discharge: confusing) | `rx_dispense_indent` / `rx_ward_issue` / `rx_ipd_return` call `ipd_lock_admission` first; if a live final bill exists the dispense is allowed (patient still in bed) but the response and slip carry "Final bill already generated — regenerate before discharge" |
| D11 | Medium | Number series keys without year | `PHS-2027-000001` would continue 2026's count | Keys `'rx_sale:'||year`, `'rx_ret:'||year`, … (A51 pattern) |
| D12 | Medium | Number assigned mid-function | `opd_next_counter` locks the **clinics row** (A51) for the rest of the transaction — would serialise OPD invoicing behind slow pharmacy work | Every RX function assigns its number as the **last** statement before return |
| D13 | Medium | Trigram index created unconditionally | Migration fails where `pg_trgm` is absent | Created only `IF EXISTS (SELECT 1 FROM pg_extension WHERE extname='pg_trgm')`; resolver falls back to prefix/ILIKE (120 pattern) |
| D14 | Low | Expiry input `MM/YYYY` only | Some injectables/vaccines print a full date | Accept `DD/MM/YYYY` (exact) or `MM/YYYY` (month end) |
| D15 | Low | W4 cited MAR route `/mar` | Wrong path (A53) | `/administrations` |

## 4. Latent defects already in production (found while tracing; not caused by RX)
| ID | Defect | Today's impact | RX impact | Fix task |
|:-|:-|:-|:-|:-|
| L1 | `delete_branch` only checks 4 tables (A54) | Deleting a branch that has IPD wards/admissions or DX sites returns **500 "Failed to delete branch"** instead of deactivating | `rx_stores.branch_id` adds one more FK that hits it | **RX-67**: on FK violation (SQLSTATE 23503) fall back to the existing deactivate path with reason "has linked records"; test with IPD ward, DX site, RX store, and a clean branch (still hard-deleted) |
| L2 | Erasure per-member fallback checks 4 OPD tables (A55) | A family member with only an IPD admission or DX accession: delete raises, loop aborts, later members unprocessed | `rx_sales`/`rx_refill_plans` add references | **RX-68**: treat 23503 on the per-member delete as "referenced → redact in place"; continue the loop; cancel and delete that patient's `rx_refill_plans`/`rx_refill_requests` (PII with no retention duty); statutory register snapshots kept (legal retention) |
Both fixes are small, change only the failure branch, and make the existing behaviour correct for IPD/DX clinics too.
They are scheduled in P1 because RX must not ship on top of them.

## 5. Loophole register (abuse and mistake cases)
| # | Attack / mistake | v1 status | Control now in the design |
|:-|:-|:-|:-|
| X1 | Same OPD Rx filled twice at two counters (or twice at one) — e.g. narcotic | partial (draft unique per store only) | `rx_complete_sale` takes `pg_advisory_xact_lock(hashtext('rx_rx:'||rx_id))`; Σ fulfilled units per Rx line ≤ prescribed units when computable. **Hard** for H1/X/narcotic/psychotropic (also when not computable: one fill per line); others: warning, override needs `RX_ADMIN` + reason |
| X2 | Same paper (external) Rx reused for Schedule X / narcotic | open | `external_rx_fingerprint = sha256(reg_no‖rx_date‖normalised patient name)` stored on the sale; reuse → **block** for X/narcotic, warn for H1 |
| X3 | Cash refund of a UPI/card sale (cash leakage) | open | refund tender = original tender split; cash only up to the cash paid; other routes need `RX_ADMIN` + reason |
| X4 | Return refunded at today's MRP or full price after a header discount | open | refund = sold net per unit (line net after allocated header discount) × units, half-up |
| X5 | Return of a voided sale / double return | partial | `returned_units ≤ qty_units` CHECK + return function locks the sale row; voided sale → 409 |
| X6 | Discount stacking (line + header) beyond cap | open | cap applies to Σ(line + allocated header) / gross; over cap needs manager override (A9) |
| X7 | Fake opening stock to cover theft later | partial | opening import: `RX_ADMIN`, only within 30 days of the store's creation and before its first non-opening movement, value logged; above `audit_approval_paise` a second approver |
| X8 | GRN inflated (free goods) to hide shrinkage | partial | GRN post shows PO variance; free packs above PO free packs flagged; GRN creator ≠ poster optional setting `grn_two_person` |
| X9 | Selling expired stock by back-dating | covered | DB uses `now()` in IST; no client date accepted |
| X10 | Nurse indents for a patient in another ward | covered | store/ward scope 404 + trigger checks order admission |
| X11 | Pharmacist dispenses more than indented | covered | CHECK `dispensed ≤ requested` |
| X12 | Narcotic ampoule diverted (given "1", used 0.5, rest pocketed) | open | empty-ampoule return control (ADVANCED-FEATURES A11) + MAR link; daily controlled audit |
| X13 | Register edited directly in SQL | covered (detect) | hash chain + nightly verify; RLS/API lockout of `anon`/`authenticated` |
| X14 | Price above MRP via manual price | covered | server re-pricing + CHECK |
| X15 | Batch recalled but already sold | open | recall trace (ADVANCED-FEATURES A6) |
| X16 | Refill reminder to a patient who never agreed | open | plan created only when the pharmacist ticks "Patient wants refill reminders" (recorded actor + time) **and** `accepts_engagement` is true at send time |
| X17 | Forged refill button payload for another patient's plan | open | payload plan must belong to the same clinic **and** the sender's phone; otherwise ignored (logged) |
| X18 | Staff sells to self at deep discount | partial | sale_kind `staff` requires `RX_ADMIN` approval of discount; staff sales report |
| X19 | Drawer-less super-admin takes cash | covered | `resolve_counter_shift(require_drawer=True)` for cash → 403 (A50) |

## 6. Concurrency and deadlock analysis
| Lock | Taken by | Order rule |
|:-|:-|:-|
| `rx_stock_balances` rows (store×batch) | every movement (trigger) | RX functions insert movements in **ascending (store_id, batch_id)** order → no cycles between RX transactions |
| `pg_advisory_xact_lock('rx_rx:'||rx_id)` | `rx_complete_sale` with an Rx | taken **before** any balance lock |
| `ipd_admissions` row (`ipd_lock_admission`) | IPD dispense/ward issue/return; IPD's own functions | taken **first** in RX IPD functions (IPD functions take it first too) |
| `pg_advisory_xact_lock('rx_reg:'…)` | register append | after balances (one register per transaction per kind, sorted by register name) |
| `clinics` row (`opd_next_counter`) | number assignment (RX and OPD/IPD) | **last** statement in every RX function (D12) |
| `opd_cashier_shifts` row | `opd_close_shift` (FOR UPDATE) | RX never locks shifts; payment insert only references the row (FK takes KEY SHARE, compatible) |
Python retries a whole RX RPC **once** on SQLSTATE `40P01` (deadlock) or `40001` (serialisation) with the same
idempotency key; a second failure returns 503 "busy, retry". Proven by RG8 (TEST-PLAN §3).

## 7. Zero-impact scenario matrix
"No change" = identical behaviour, payloads and SQL results to today; proven by the test in the last column.

### 7.1 Clinic configurations
| Clinic | Pharmacy flag | Expected | Proof |
|:-|:-|:-|:-|
| WhatsApp-only (no OPD) | absent | no change; `pharmacy_enabled` toggle → 400 (needs OPD) | `test_rx_feature_gate.py` |
| OPD only | absent | no change; shift close identical (D1) | RG5 + `test_close_shift_unchanged_without_rx` |
| OPD + IPD | absent | no change; MAR insert, charges, final bill identical; no trigger added to IPD tables (D2, D9) | RG5 unedited, `test_non_rx_clinic_zero_rx_queries` |
| DX only | absent | no change | RG4/DX suites |
| Enterprise (wildcard plan) | absent | **not** enabled by wildcard | `test_rx_feature_gate.py::test_wildcard_never_enables` |
| Corporate partner account | any | 400 | same file |
| OPD + Pharmacy | ACTIVE | POS, GRN, transfers, registers; IPD routes 403 `ipd_not_enabled` | P4 suites |
| OPD + IPD + Pharmacy | ACTIVE | full | all suites |
| Pharmacy WIND_DOWN | — | no new sales/GRNs/indents; dispensing of already-raised indents, returns, register export, audits allowed | `test_rx_feature_gate.py::test_wind_down_matrix` |
| IPD WIND_DOWN + Pharmacy ACTIVE | — | admitted patients still get indents/dispense (D6) | `test_rx_ipd.py::test_ipd_wind_down_still_dispenses` |

### 7.2 Existing workflows with RX data present
| Existing workflow | With RX rows | Proof |
|:-|:-|:-|
| Delete branch (`DELETE /admin/branches/{id}`) | deactivates with reason instead of 500 (L1 fix) | `test_branch_delete_fk_fallback` (RX-67) |
| DPDP erasure of a patient | completes; RX-referenced family members redacted; refill plans removed; registers retained (L2 fix) | `test_erasure_with_rx_references` (RX-68) |
| Doctor delete | unaffected — RX stores prescriber **snapshots**, no FK to `doctors` | `test_doctor_delete_with_rx_sales` |
| Staff (clinic_admins) delete | unaffected — RX stores actor ids/names, **no FK** to `clinic_admins` | `test_staff_delete_with_rx_rows` |
| Ward deactivate | RX ward store stays; new ward-issue refused when ward inactive (`rx_ward_inactive`) | `test_rx_ipd.py` |
| Rx superseded after a draft sale was created | completion 409 `rx_prescription_superseded`; no stock moved | `test_rx_pos.py` |
| Doctor discontinues medication order | open indent lines show "order stopped"; dispense 409 | `test_rx_ipd.py::test_discontinued_order_blocks_dispense` |
| Admission cancel | only from `pending`; pending admissions cannot have orders → no RX rows possible | trigger 111:573 |
| Discharge revert / final bill void | pharmacy charges behave like any posted charge (released by void) | RG5 + `test_rx_ipd.py::test_bill_void_releases_pharmacy_charges` |
| Package covering `pharmacy` | dispensed medicines shown as covered on the bill (A62) — existing, intended behaviour; dispense slip shows "Covered by package" | `test_rx_ipd.py::test_package_covers_pharmacy` |
| Disable OPD with pharmacy on | 409 (D7) | `test_rx_feature_gate.py` |
| Owner purge / clinic deletion | rx purged first; succeeds (D8) | `test_migration_rx_m1.py::test_platform_purge_order` |
| Global STOP on WhatsApp | patient `opted_in=false` → refill job sends nothing; RX does not intercept STOP (D3) | `test_rx_refills.py::test_global_stop_suppresses` |
| Other template buttons (dental review, check-ins) | unchanged — RX claims only the `rx_refill` prefixes | `test_rx_refills.py::test_other_payloads_untouched`, existing dental tests |
| Migrations rolled back (code reverted first) | each down migration refuses while RX data exists for an unpurged clinic; 138 down restores the 116 `opd_close_shift` | RG6 |
| GSTR-1 export, non-RX clinic | byte-identical | golden file (P10) |

## 8. Where each fix landed
| Fix | Docs changed |
|:-|:-|
| D1 | ARCHITECTURE §7/§8, DATA-MODEL §5, GAP G9, status RX-34, TEST-PLAN §6, AGENT-PROMPT §2 |
| D2 | DATA-MODEL §6/§11/§12, ADR-RX-04, ARCHITECTURE §1/§7, WORKFLOWS W4, API §6, UI §3, AUDIT A19, status RX-37/39/40, AGENT-PROMPT §2 |
| D3 | DATA-MODEL §9, WORKFLOWS W10, KRIYA-KETU §4, SECURITY §5, TEST-PLAN P8, status RX-57/58 |
| D4, D5, D11, D12 | DATA-MODEL §0/§5, ADR-RX-10, ARCHITECTURE §4 |
| D6, D7 | ADR-RX-09, API §1/§6 |
| D8 | DATA-MODEL §11, status RX-08 |
| D9, D10 | ARCHITECTURE §6/§7/§8, DATA-MODEL §6 |
| D13, D14 | DATA-MODEL §2, WORKFLOWS W1 |
| L1, L2 | status RX-67/RX-68, AGENT-PROMPT §2 |
| X1–X19 | SECURITY §1/§4, WORKFLOWS W2/W3/W5a, DATA-MODEL §5, ADVANCED-FEATURES |

## 9. Residual risks (accepted, owner-visible)
| Risk | Why accepted | Watch |
|:-|:-|:-|
| Meta may classify refill reminders as **marketing**, not utility | template category is Meta's decision; cost and opt-in rules then differ | ROLLOUT §2 item 10: submit as utility; if re-categorised, refills stay opt-in only and the owner is told the per-message cost |
| Register rule numbers/formats vary by state | legal text not in code | O-4 sign-off before go-live |
| Supabase statement timeout on very large GRN posts (>200 lines) | rare | GRN line cap 200; post splits nothing — documented limit |
| Shared `clinics` row lock for numbering | existing design of all series | D12 keeps hold time minimal; P11 latency check |
