# Kriya Pharmacy OS — Architecture Decision Records

> Part of the Pharmacy OS set: start at [status.md](status.md). Each ADR: context → alternatives → decision →
> consequences → evidence (the test that proves it, and the phase that writes it). Status **Accepted** unless marked.
> Open owner decisions are at the end; each has a default in force so no phase blocks.

## ADR-RX-01 — Append-only stock movements, with a trigger-maintained balance projection
- **Context.** A hospital pharmacy loses money and fails inspections through un-explainable stock: a counter that was
  "edited" leaves no trail. Every competitor stores a mutable `current_stock` column (COMPETITIVE-ANALYSIS §3). The
  brief requires stock = sum of movements and a database-level ban on negative stock.
- **Alternatives.**
  (a) Mutable `qty` on a batch-store row, updated by application code. Fast, but untraceable and race-prone under
  parallel checkouts (read-modify-write).
  (b) Ledger only; compute `SUM(qty)` on every read and on every insert to check negativity. Correct, but O(n) per
  check, and two concurrent inserts can each see "enough" stock (write skew) unless every out-movement takes an
  advisory lock on the store×batch.
  (c) **Ledger as the source of truth + a balance projection written only by an AFTER INSERT trigger on the ledger,
  with `CHECK (qty_units >= 0)`.**
- **Decision.** (c). `rx_stock_movements` is append-only and insert-only-through-`rx_*`-functions. The trigger
  `rx_sm_apply` upserts `rx_stock_balances` with `ON CONFLICT DO UPDATE SET qty = qty + EXCLUDED.qty`; the conflict
  path row-locks the store×batch balance, so concurrent movements on the same batch serialise and the CHECK rejects
  the one that would go negative. Nothing else may write `rx_stock_balances` (`rx_guard_balance` checks
  `pg_trigger_depth()`). Corrections are new movements (`adjust_in/out` with reason), never edits.
- **Consequences.** O(1) stock reads; stock is provably the ledger sum (nightly `rx_ledger_check`); hot items serialise
  per batch only (different batches of the same item sell in parallel). Projection drift is impossible except by a
  superuser bypassing triggers — detected by the nightly check.
- **Evidence.** P2 `tests/test_migration_rx_m3.py::test_balance_equals_ledger_sum`,
  `::test_direct_balance_update_refused`, `::test_movement_outside_function_refused`;
  P4 `tests/test_rx_concurrency.py::test_parallel_checkouts_never_negative` (50 concurrent connections, 1 unit left →
  exactly one success).

## ADR-RX-02 — FEFO recommendation with a hard expiry lock
- **Context.** Selling an expired drug is an offence under the Drugs and Cosmetics Act and a patient-safety event.
  FIFO (by receipt) leaves short-dated stock on the shelf to expire.
- **Alternatives.** (a) FIFO. (b) FEFO advisory only. (c) **FEFO as default pick + hard DB lock on expired/blocked
  batches + soft lock on short shelf life.**
- **Decision.** (c). `rx_v_fefo` ranks on-hand, unblocked, unexpired batches by `expiry_date, created_at`. POS and
  dispense auto-pick rank 1 (splitting across batches when rank 1 is short). Choosing a later batch requires
  `fefo_override_reason` (setting `fefo_override_requires_reason`, default on). The movement trigger
  `rx_sm_expiry_lock` refuses any out-movement of type sale/dispense/transfer/ward_issue on a batch whose
  `expiry_date < rx_ist_date(now())` or `is_blocked` — regardless of what the API sent. Batches expiring within
  `min_shelf_days_at_sale` (default 30) of today, or before the prescribed course ends, raise a **warning the
  pharmacist must acknowledge** (not a block: a 5-day antibiotic course from a batch with 20 days left is fine).
  Expiry is stored as the last day of the printed month; "today" is IST.
- **Consequences.** Expired stock can only leave by supplier return or write-off. Midnight IST is the cut-over; a sale
  drafted at 23:59 and completed at 00:01 on expiry-day+1 fails at completion (correct).
- **Evidence.** P2 `test_migration_rx_m3.py::test_expired_batch_out_refused` (freeze IST clock),
  `::test_writeoff_of_expired_allowed`; P4 `test_rx_pos.py::test_fefo_pick_and_split`,
  `::test_fefo_override_needs_reason`.

## ADR-RX-03 — Multi-store hierarchy: central store, retail counter, ward sub-stores, OT/emergency trays
- **Context.** Hospitals hold stock in several physical places with different custodians; stock must be known per
  place and moved with acknowledgment.
- **Alternatives.** (a) One stock pool per clinic with a "location" label. (b) **Stores as first-class rows; every
  movement names one store; transfers are paired out/in movements with a receipt step.**
- **Decision.** (b). `rx_stores.store_type ∈ central | retail_counter | ward_substore | ot_tray | emergency_tray`;
  `parent_store_id` defines replenishment. Only `retail_counter` may sell (`can_sell`); only stores with
  `can_receive_grn` take GRNs. A transfer debits the source at **dispatch** and credits the destination at **receipt**;
  short receipt is written off against the source with a reason. One central store per branch (partial unique).
  Ward sub-stores are tied to an `ipd_wards` row; a nurse may issue from their ward's sub-store against an active
  medication order (`ward_issue_out`) — this posts the IPD charge the same way as a pharmacy dispense.
- **Consequences.** In-transit stock is "nowhere" between dispatch and receipt (shown on the Transfers page as
  "in transit" from `rx_transfers` status, not as a store). A single-counter clinic runs one `retail_counter` store
  with `can_receive_grn = true` and never sees the hierarchy.
- **Evidence.** P6 `test_rx_transfers.py::test_dispatch_debits_receipt_credits`, `::test_short_receipt_writeoff`,
  `::test_substore_cannot_sell`.

## ADR-RX-04 — IPD indent → dispense → charge → MAR, linked but never merged
- **Context.** Today `ipd_orders` medication rows have no quantity and no stock link; MAR (`ipd_med_administrations`)
  records doses; nothing charges medicines except manual `ipd_charges` with category `pharmacy` (AUDIT A16–A22).
- **Alternatives.** (a) Charge at doctor order time (115 pattern for tests). Wrong: medicines are charged for what
  was supplied, not what was ordered, and PRN orders may never be given. (b) Charge at MAR "given". Wrong: a strip is
  supplied to the ward once and many doses are given; returns would be impossible. (c) **Charge at dispense, link MAR
  to the dispense line.**
- **Decision.** (c). Nurse raises `rx_ipd_indents` with lines referencing **active medication** `ipd_orders`
  (trigger-checked). Pharmacist dispenses with `rx_dispense_indent`: stock movement + `ipd_charges` row
  (`category='pharmacy'`, `source='pharmacy'`, `source_ref='rx:dl:<line>'`, `posted` when `ipd_autopost`) in one
  transaction. The nurse administers on the existing MAR and may pick the dispensed batch, stored in the RX-owned link table
  `rx_mar_links` (no IPD table is altered — VERIFICATION D2) for recall traceability. Unused stock comes back via `ipd_return_in` → charge void + re-issue for the kept quantity (unbilled)
  or a credit note on the final IPD invoice (billed). Discharge medicines are an `rx_sales` with
  `sale_kind='ipd_discharge_meds'`, payable at the counter or added to the IPD bill (`mode='ipd_bill'` → a pharmacy
  charge on the admission).
- **Consequences.** Pharmacy writes exactly three existing objects: `ipd_charges` inserts (widened `source` CHECK),
  `ipd_charges` voids through `ipd_void_charge`, and nothing on `ipd_orders`/MAR content. The 115 investigation path
  is untouched (it skips medication orders, 115:519). `ipd_charges.quantity ≤ 999` forces splitting of very large
  lines (DATA-MODEL §6).
- **Evidence.** P5 `test_rx_ipd.py::test_indent_requires_active_med_order`, `::test_dispense_posts_charge_once`
  (retry ×3 → one charge), `::test_discontinued_order_blocks_new_indent_line`, `::test_return_voids_and_reissues`,
  `::test_return_after_bill_issues_credit_note`; 111/115/116/117 suites green unedited.

## ADR-RX-05 — Statutory registers: Schedule H, H1, X and narcotics as a hash-chained append-only ledger
- **Context.** Drugs and Cosmetics Rules, 1945 require retail licensees to record Schedule H1 sales (prescriber,
  patient, drug, quantity) and retain the record for three years, and Schedule X sales/purchases with stricter
  custody; hospitals handling narcotics (morphine, fentanyl, pethidine) maintain stock registers under the NDPS Act
  and their state's rules for Recognised Medical Institutions. Drug Inspectors ask for the register in print.
  (Exact rule numbers and register formats are confirmed by the pilot's legal reviewer — ROLLOUT §3, owner item O-4.)
- **Alternatives.** (a) Report generated on demand from sales. Breaks when a sale is voided or an item's schedule
  changes later. (b) **A dedicated register table written in the same transaction as the movement, with snapshots,
  gapless sequence and a hash chain.**
- **Decision.** (b). `rx_register_entries` per (clinic, register) with `seq` gapless under an advisory lock,
  `row_hash = sha256(prev_hash || canonical row)`. Outward entries require `prescriber_snapshot ?
  'registration_number'` (CHECK) — copied from `opd_prescriptions.signer_snapshot` (103:355) for in-house Rx, or
  entered from the paper Rx for external prescriptions. H1/X/narcotic outward entries require a patient snapshot. A
  voided sale writes a `return` entry, never deletes. Schedule X/narcotic items additionally: per-unit dispensing,
  `RX_CONTROLLED` permission, running balance per store, daily balance check in the controlled audit scope.
  Export: PDF (A4 landscape, page-numbered, clinic + licence header) and CSV, per register and date range.
- **Consequences.** Register rows survive item edits and sale voids; tampering by direct SQL is detectable by
  `rx_register_verify`. Retention: rows are never deleted except by full owner purge (SECURITY §6).
- **Evidence.** P7 `test_rx_registers.py::test_h1_sale_writes_entry_with_prescriber`,
  `::test_missing_reg_no_blocks_sale`, `::test_void_writes_return_entry`, `::test_chain_detects_tamper`,
  `::test_seq_gapless_under_concurrency`.

## ADR-RX-06 — Zero-LLM clinical boundary for medication handling
- **Context.** AGENTS.md §1.5 and NMC rules: AI must not prescribe, change doses or substitute molecules. Pharmacy is
  the place where that temptation is highest ("suggest an alternative").
- **Decision.** No module under `app/services/rx*.py`, `app/routers/rx.py` or the RX scheduler jobs may import
  `ai_engine`, `ai_gateway`, `hybrid_search` or any LLM client — enforced by `tests/test_rx_no_llm.py` (AST, same as
  `test_dx_no_llm.py`). Prescription → item mapping, substitution candidates, quantity calculation, refill dates and
  Ketu stock answers are deterministic. Substitution candidates are only items with the **same `generic_id`**
  (molecule + strength + form); the pharmacist chooses; the line records `substitution_reason`. A different molecule,
  strength or form is never offered. The dispense quantity is never "suggested" from free-text frequency outside the
  closed map (DATA-MODEL §9). Refill messages contain item name, last fill date and a reply keyword — no dosing text;
  they pass `validate_llm_output`-equivalent static template review because they are approved WhatsApp templates.
- **Consequences.** Kriya never offers clinical judgement in pharmacy; the "smart" parts are search, FEFO, stock
  math and reminders.
- **Evidence.** P1 `tests/test_rx_no_llm.py`; P4 `test_rx_pos.py::test_substitute_candidates_same_generic_only`.

## ADR-RX-07 — Split dispensing and out-of-stock substitution
- **Context.** OPD Rx lines are free text (AUDIT A4); stock may cover part of a line; a brand may be out while the
  same generic is in.
- **Decision.**
  1. **Mapping.** For each signed Rx line: exact alias hit (`rx_item_aliases.alias_norm = rx_normalize(drug_name ||
     ' ' || strength)`) → else barcode/scan → else trigram top-5 over `brand_name` filtered by `dosage_form =
     formulation`. The pharmacist confirms; confirmation writes an alias (`pharmacist_confirmed`), so the second time
     it is instant.
  2. **Split across batches.** One Rx line may become several sale lines (batches); FEFO order.
  3. **Partial fulfilment.** If stock < required, the sale fulfils what is available; `rx_rx_fulfilments` records
     units; the Rx remains "partially fulfilled" for 7 days and is offered again on the next scan; the receipt prints
     "Balance: n units pending".
  4. **Substitution.** Same `generic_id` only, pharmacist-initiated, reason mandatory (`out_of_stock`,
     `patient_request_cost`, `formulary`), printed on the receipt ("Dispensed X in place of Y — same composition");
     Schedule H1/X substitution additionally needs `RX_CONTROLLED`. If the prescriber wrote "no substitution"
     (free-text instruction matched deterministically by `\bno\s+sub`), substitution is blocked.
- **Evidence.** P4 `test_rx_pos.py::test_alias_learned_on_confirm`, `::test_partial_fulfilment_carries_balance`,
  `::test_no_sub_instruction_blocks_substitution`.

## ADR-RX-08 — Tax and GST accounting
- **Context.** Retail medicine prices are **MRP, inclusive of GST**; the legal maximum (DPCO 2013 / Legal Metrology):
  never sell above MRP. OPD billing is tax-exclusive (AUDIT A9). Inpatient medicines are part of the composite supply
  of healthcare (exempt). GST rates for medicines change by notification (the 2025 rate rationalisation moved most
  medicines to 5% and some to nil) — so rates are data, never code.
- **Decision.**
  - **Rates are per item** (`rx_items.tax_rate_bps`, CHECK identical to 117:55) and **snapshotted per line**.
  - **OPD/retail (inclusive).** `line_gross = rx_unit_price(mrp_pack, pack_size, units)` (half-up integer);
    `line_net = line_gross − line_discount`; `taxable = (line_net × 10000 + (10000 + rate)/2) / (10000 + rate)`
    (half-up); `tax = line_net − taxable`; `cgst = tax / 2`, `sgst = tax − cgst`, `igst = 0` (counter sale is
    intra-state). The bill shows taxable value and CGST/SGST per rate slab, and HSN per line. Header discount is
    allocated to lines pro rata before the split, so the tax always follows the price actually charged.
  - **GSTIN gate.** If `opd_clinic_gstin(clinic)` (117:47) is NULL the sale is a **bill of supply**: no tax lines, and
    `taxable = line_net` (the clinic cannot charge GST, same rule as 117:109). If present, `is_tax_invoice = true` and
    the GSTIN prints.
  - **Rounding.** Net rounds to the rupee when `round_off_to_rupee`; `round_off_paise ∈ [−99, 99]` shown on the bill.
  - **MRP ceiling.** `line_gross ≤ MRP-proportional value` (CHECK on `rx_sale_items`); discounts only reduce.
  - **IPD.** Dispensed medicines post to `ipd_charges` with `tax_rate_bps = 0`, `hsn_sac = item.hsn_code`,
    `unit_price_paise = rx_unit_price(mrp_pack, pack_size, 1)` (MRP per unit) unless `rx_settings.ipd_price_basis =
    'cost_plus'` (owner decision O-2). The final IPD bill (an `opd_invoices` row) stays exempt as today.
  - **GRN (purchase).** Tax-exclusive supplier rates; CGST/SGST vs IGST by `is_interstate`; input tax recorded on the
    GRN for the accountant (ITC report); landed cost excludes recoverable GST when the clinic has a GSTIN.
  - **Reports.** RX-52 adds `rx_sales` and `rx_credit_notes` to the GSTR-1 export (B2C small by rate, HSN summary);
    purchase register export from `rx_grns`.
- **Consequences.** One integer formula on both sides (Python mirror in `app/services/rx_tax.py`, property-tested
  against SQL for every rate and 10⁵ random amounts).
- **Evidence.** P4 `test_rx_tax.py::test_inclusive_split_matches_sql`, `::test_no_gstin_bill_of_supply`,
  `::test_header_discount_allocation_sums`, `::test_never_above_mrp`.

## ADR-RX-09 — Feature gate and lifecycle (OFF → ACTIVE → WIND_DOWN → OFF)
- **Decision.** `OPT_IN_FEATURES["pharmacy_enabled"]`; `rx_eligible(clinic) = opd_enabled(clinic)`; `rx_access(clinic)`
  → `ACTIVE` when `features.pharmacy_enabled is True` and eligible; `WIND_DOWN` when `clinics.rx_state='WIND_DOWN'`;
  else `None`. IPD dispensing additionally needs `ipd_access(clinic) is not None` (WIND_DOWN keeps admitted patients
  supplied — VERIFICATION D6). The platform refuses disabling OPD while `rx_state ≠ 'OFF'`, and treats
  `pharmacy_enabled = None` exactly like `false` (D7). Disable with open drafts, open
  indents or undispatched transfers → 409 with a preview; confirm → `WIND_DOWN` (no new sales/GRNs; returns, register
  export and stock audit still work) → `OFF` when the preview is empty. Re-enable restores ACTIVE with data intact.
  Never `has_feature(clinic, "pharmacy_enabled")` (enterprise wildcard must not switch it on).
- **Evidence.** P1 `test_rx_feature_gate.py` (matrix: plan × feature × state × route).

## ADR-RX-10 — Numbering
`PHS-` sales (width 6), `PRT-` returns, `PCN-` credit notes, `PO-`, `GRN-`, `IND-`, `STR-`, `AUD-` — all from
`opd_next_counter(clinic, 'rx_<kind>:'||year)` (103:195; year-in-key like `inv:`/`rct:`) under its row lock on the
`clinics` row: gapless, per clinic, per IST year. Drafts have no number (like 103 invoices). The number is assigned
as the **last** statement of the completing function so the `clinics` row lock is held for microseconds and never
delays OPD invoicing (VERIFICATION D11/D12).

## Open owner decisions (defaults in force)
| ID | Decision | Default |
|:-|:-|:-|
| O-1 | Credit (on-account) OPD sales for corporate/staff | Off (RX-D2 deferred); POS is pay-at-counter |
| O-2 | IPD medicine price basis | MRP per unit (`ipd_price_basis='mrp'`) |
| O-3 | Auto-post IPD pharmacy charges | On (`ipd_autopost=true`) |
| O-4 | Legal confirmation of register formats and rule citations for the pilot state | Required before go-live |
| O-5 | Refill reminders on by default for chronic items | Off; clinic enables (`refill_enabled`) after template approval |
| O-6 | Home delivery of refills | Pickup only until the clinic confirms a delivery licence/partner |
